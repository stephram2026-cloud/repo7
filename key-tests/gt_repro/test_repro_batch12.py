"""C8: shipping method deleted while cached in CheckoutDelivery -> FK error at completion."""

import pytest
from django.db import IntegrityError

from saleor.checkout import complete_checkout as cc
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout
from saleor.graphql.checkout.tests.mutations.test_checkout_complete_with_transactions import (
    prepare_checkout_for_test,
)
from saleor.order.models import Order
from saleor.plugins.manager import get_plugins_manager
from saleor.shipping.models import ShippingMethod


@pytest.mark.django_db(transaction=True)
def test_c8_deleted_shipping_method_crashes_completion(
    checkout_with_item,
    address,
    checkout_delivery,
    transaction_item_generator,
    transaction_events_generator,
):
    delivery = checkout_delivery(checkout_with_item)
    checkout = prepare_checkout_for_test(
        checkout_with_item,
        address,
        address,
        delivery,
        transaction_item_generator,
        transaction_events_generator,
    )
    # deliveries are fresh (inside the 24h TTL), so completion will not refresh them
    from django.conf import settings
    from django.utils import timezone

    Checkout.objects.filter(pk=checkout.pk).update(
        delivery_methods_stale_at=timezone.now() + settings.CHECKOUT_DELIVERY_OPTIONS_TTL
    )
    # staff deletes the shipping method (shippingPriceDelete)
    ShippingMethod.objects.filter(pk=delivery.built_in_shipping_method_id).delete()

    manager = get_plugins_manager(allow_replica=False)
    checkout = Checkout.objects.get(pk=checkout.pk)
    lines, _ = fetch_checkout_lines(checkout)
    info = fetch_checkout_info(checkout, lines, manager)

    with pytest.raises(IntegrityError, match="shipping_method"):
        cc.complete_checkout(
            manager=manager,
            checkout_info=info,
            lines=lines,
            payment_data={},
            store_source=False,
            user=None,
            app=None,
        )
    assert Order.objects.count() == 0
    assert Checkout.objects.filter(pk=checkout.pk).exists()
