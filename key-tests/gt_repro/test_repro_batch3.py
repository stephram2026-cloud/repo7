"""R10: a failed/timed-out shipping app call is persisted as "no methods" for the TTL."""

import datetime
from unittest import mock

from django.utils import timezone

from saleor.checkout.delivery_context import (
    assign_delivery_method_to_checkout,
    fetch_shipping_methods_for_checkout,
    get_or_fetch_checkout_deliveries,
)
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery


def _info(checkout, manager):
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


@mock.patch("saleor.webhook.transport.synchronous.transport.send_webhook_request_sync")
def test_r10_failed_shipping_app_call_cached_for_full_ttl(
    mocked_webhook, checkout_with_item, plugins_manager, address, shipping_app, settings
):
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])

    # 1. app answers, customer picks the external method
    mocked_webhook.return_value = [
        {"id": "abc", "name": "External", "amount": 10, "currency": checkout.currency}
    ]
    checkout_info, lines = _info(checkout, plugins_manager)
    deliveries = fetch_shipping_methods_for_checkout(
        checkout_info, requestor=None
    ).get()
    external = next(d for d in deliveries if d.is_external)
    assign_delivery_method_to_checkout(checkout_info, lines, plugins_manager, external)

    # 2. deliveries are stale (e.g. cart changed) and the app request FAILS
    #    (send_webhook_request_sync returns None on timeout / 5xx / broken circuit)
    Checkout.objects.filter(pk=checkout.pk).update(
        delivery_methods_stale_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    mocked_webhook.return_value = None
    checkout = Checkout.objects.get(pk=checkout.pk)
    checkout_info, lines = _info(checkout, plugins_manager)
    before = timezone.now()
    deliveries = get_or_fetch_checkout_deliveries(checkout_info, requestor=None).get()

    # BUG: the failure is treated as "app returned no methods"
    assert not [d for d in deliveries if d.is_external]
    assert CheckoutDelivery.objects.get(pk=external.pk).is_valid is False
    checkout = Checkout.objects.get(pk=checkout.pk)
    assert checkout.assigned_delivery_id == external.pk
    # ...and cached for the whole TTL (24h by default)
    assert checkout.delivery_methods_stale_at >= (
        before + settings.CHECKOUT_DELIVERY_OPTIONS_TTL
    )

    # 3. app is healthy again, but nothing asks it any more
    mocked_webhook.reset_mock()
    mocked_webhook.return_value = [
        {"id": "abc", "name": "External", "amount": 10, "currency": checkout.currency}
    ]
    checkout_info, lines = _info(checkout, plugins_manager)
    deliveries = get_or_fetch_checkout_deliveries(checkout_info, requestor=None).get()
    mocked_webhook.assert_not_called()
    assert not [d for d in deliveries if d.is_external]
