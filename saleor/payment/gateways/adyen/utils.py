import logging
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

import Adyen
import graphene
from babel.numbers import get_currency_precision
from django.conf import settings
from django_countries.fields import Country

from ....checkout.calculations import checkout_line_total
from ....checkout.models import Checkout
from ....core.prices import quantize_price
from ....discount.utils import fetch_active_discounts
from ... import PaymentError
from ...interface import PaymentData

logger = logging.getLogger(__name__)


def convert_adyen_price_format(value: str, currency: str):
    value = Decimal(value)
    precision = get_currency_precision(currency)
    number_places = Decimal(10) ** -precision
    return value * number_places


def get_price_amount(value: Decimal, currency: str):
    """Adyen doesn't use values with comma.

    Take the value, discover the precision of currency and multiply value by
    Decimal('10.0'), then change quantization to remove the comma.
    """
    value = quantize_price(value, currency=currency)
    precision = get_currency_precision(currency)
    number_places = Decimal("10.0") ** precision
    value_without_comma = value * number_places
    return str(value_without_comma.quantize(Decimal("1")))


def api_call(request_data: Optional[Dict[str, Any]], method: Callable) -> Adyen.Adyen:
    try:
        return method(request_data)
    except (Adyen.AdyenError, ValueError, TypeError) as e:
        logger.error(f"Unable to process the payment: {e}")
        raise PaymentError("Unable to process the payment request.")


def request_data_for_payment(
    payment_information: "PaymentData",
    return_url: str,
    merchant_account: str,
    origin_url: str,
) -> Dict[str, Any]:
    payment_data = payment_information.data or {}

    if not payment_data.pop("is_valid", True):
        raise PaymentError("Payment data are not valid")

    extra_request_params = {}
    if "browserInfo" in payment_data:
        extra_request_params["browserInfo"] = payment_data["browserInfo"]
    if "billingAddress" in payment_data:
        extra_request_params["billingAddress"] = payment_data["billingAddress"]
    if "shopperIP" in payment_data:
        extra_request_params["shopperIP"] = payment_data["shopperIP"]
    if (
        "browserInfo" in extra_request_params
        and "billingAddress" in extra_request_params
    ):
        # Replace this assigment. Add note that customer_ip_address has incorrect name
        # Add to dashboard config the flow to combine channel with url like:
        # web1:https://shop.com, web2:https://shop1.com
        extra_request_params["origin"] = origin_url

    payment_method = payment_data["paymentMethod"]

    request_data = {
        "amount": {
            "value": get_price_amount(
                payment_information.amount, payment_information.currency
            ),
            "currency": payment_information.currency,
        },
        "reference": payment_information.payment_id,
        "paymentMethod": payment_method,
        "returnUrl": return_url,
        "merchantAccount": merchant_account,
        **extra_request_params,
    }

    method = payment_method.get("type", [])
    if "klarna" in method:
        request_data = append_klarna_data(payment_information, request_data)

    return request_data


def append_klarna_data(payment_information: "PaymentData", payment_data: dict):
    _type, payment_pk = graphene.Node.from_global_id(payment_information.payment_id)
    checkout = Checkout.objects.filter(payments__id=payment_pk).first()

    if not checkout:
        raise PaymentError("Unable to calculate products for klarna")

    lines = checkout.lines.prefetch_related("variant").all()
    discounts = fetch_active_discounts()
    currency = payment_information.currency

    payment_data["shopperLocale"] = get_shopper_locale_value()
    payment_data["shopperReference"] = payment_information.customer_email
    payment_data["countryCode"] = checkout.get_country()
    payment_data["shopperEmail"] = payment_information.customer_email
    line_items = []
    for line in lines:
        total = checkout_line_total(line=line, discounts=discounts)
        total_gross = total.gross.amount
        total_net = total.net.amount
        tax_amount = total.tax.amount
        line_data = {
            "quantity": line.quantity,
            "amountExcludingTax": get_price_amount(total_net, currency),
            "taxPercentage": round(tax_amount / total_gross * 100),
            "description": line.variant.product.description,
            "id": line.variant.sku,
            "taxAmount": get_price_amount(tax_amount, currency),
            "amountIncludingTax": get_price_amount(total_gross, currency),
        }
        line_items.append(line_data)
    payment_data["lineItems"] = line_items
    return payment_data


def get_shopper_locale_value():
    # Remove this function when "shopperLocale" will come from frontend site
    language_code_to_shopper_locale_value = {
        # https://docs.adyen.com/checkout/components-web/
        # localization-components#change-language
        "zh": "zh_CN",
        "da": "da_DK",
        "nl": "nl_NL",
        "en": "en_US",
        "fi": "fi_FI",
        "fr": "fr_FR",
        "de": "de_DE",
        "it": "it_IT",
        "ja": "ja_JP",
        "ko": "ko_KR",
        "no": "no_NO",
        "pl": "pl_PL",
        "pt": "pt_BR",
        "ru": "ru_RU",
        "es": "es_ES",
        "sv": "sv_SE",
    }
    default_language_code = settings.LANGUAGE_CODE
    return language_code_to_shopper_locale_value.get(default_language_code, "en_US")


def request_data_for_gateway_config(
    checkout: "Checkout", merchant_account
) -> Dict[str, str]:
    address = checkout.billing_address or checkout.shipping_address

    country = address.country if address else None
    if country:
        country_code = country.code
    else:
        country_code = Country(settings.DEFAULT_COUNTRY).code
    channel = checkout.get_value_from_metadata("channel", "web")
    return {
        "merchantAccount": merchant_account,
        "countryCode": country_code,
        "channel": channel,
    }


def request_for_payment_refund(
    payment_information: "PaymentData", merchant_account, token
) -> Dict[str, Any]:
    return {
        "merchantAccount": merchant_account,
        "modificationAmount": {
            "value": get_price_amount(
                payment_information.amount, payment_information.currency
            ),
            "currency": payment_information.currency,
        },
        "originalReference": token,
        "reference": payment_information.payment_id,
    }


def request_for_payment_capture(
    payment_information: "PaymentData", merchant_account, token
) -> Dict[str, Any]:
    return {
        "merchantAccount": merchant_account,
        "modificationAmount": {
            "value": get_price_amount(
                payment_information.amount, payment_information.currency
            ),
            "currency": payment_information.currency,
        },
        "originalReference": token,
        "reference": payment_information.payment_id,
    }
