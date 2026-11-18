"""C3 (real concurrency): two checkouts sharing one gift card complete at once."""

import threading
from decimal import Decimal

import pytest
from django.db import connection

from saleor.checkout import complete_checkout as cc
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutLine
from saleor.graphql.checkout.tests.mutations.test_checkout_complete_with_transactions import (
    prepare_checkout_for_test,
)
from saleor.order import utils as order_utils
from saleor.order.models import Order
from saleor.plugins.manager import get_plugins_manager
from saleor.warehouse.models import Stock
from saleor.tests.utils import prepare_test_db_connections


@pytest.mark.django_db(transaction=True)
def test_judge_k13_two_checkouts_sharing_gift_card_complete_concurrently(
    checkout_with_item,
    gift_card,
    address,
    checkout_delivery,
    transaction_item_generator,
    transaction_events_generator,
):
    variant = checkout_with_item.lines.get().variant
    Stock.objects.filter(product_variant=variant).update(quantity=100)
    gift_card.initial_balance_amount = Decimal(1000)
    gift_card.current_balance_amount = Decimal(1000)
    gift_card.save()

    checkout_a = checkout_with_item
    checkout_b = Checkout.objects.create(
        channel=checkout_a.channel, currency=checkout_a.currency, email="b@example.com"
    )
    CheckoutLine.objects.create(
        checkout=checkout_b,
        variant=variant,
        quantity=1,
        currency=checkout_a.currency,
        undiscounted_unit_price_amount=checkout_a.lines.get().undiscounted_unit_price_amount,
    )
    pks = []
    for checkout in (checkout_a, checkout_b):
        checkout.gift_cards.add(gift_card)
        prepare_checkout_for_test(
            checkout,
            address.get_copy(),
            address.get_copy(),
            checkout_delivery(checkout),
            transaction_item_generator,
            transaction_events_generator,
        )
        pks.append(checkout.pk)

    barrier = threading.Barrier(2, timeout=20)
    original = order_utils.add_gift_cards_to_order
    results = {}

    def synced(*args, **kwargs):
        # both requests hold their own checkout row lock and have already computed
        # the order total from the gift card balance when they get here
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return original(*args, **kwargs)

    def complete(name, pk):
        prepare_test_db_connections()
        try:
            manager = get_plugins_manager(allow_replica=False)
            checkout = Checkout.objects.get(pk=pk)
            lines, _ = fetch_checkout_lines(checkout)
            info = fetch_checkout_info(checkout, lines, manager)
            order, _, _ = cc.complete_checkout(
                manager=manager,
                checkout_info=info,
                lines=lines,
                payment_data={},
                store_source=False,
                user=None,
                app=None,
            )
            results[name] = ("order", order.pk)
        except Exception as e:  # noqa: BLE001
            results[name] = (type(e).__name__, str(e)[:400])
        finally:
            from django.db import connections
            connections.close_all()

    order_utils.add_gift_cards_to_order = synced
    try:
        threads = [
            threading.Thread(target=complete, args=("a", pks[0])),
            threading.Thread(target=complete, args=("b", pks[1])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
    finally:
        order_utils.add_gift_cards_to_order = original

    gift_card.refresh_from_db()
    orders = list(Order.objects.all())
    print("C3 results:", results)
    print(
        "C3 orders:",
        [
            (o.total_gross_amount, o.undiscounted_total_gross_amount, o.charge_status)
            for o in orders
        ],
        "gift_card_deducted:",
        Decimal(1000) - gift_card.current_balance_amount,
    )
    assert results
    kinds = sorted(v[0] for v in results.values())
    assert kinds == ['OperationalError', 'order'], results
    assert 'deadlock detected' in [v for v in results.values() if v[0]=='OperationalError'][0][1]
