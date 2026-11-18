"""R12: checkoutLinesUpdate pairs variants with line inputs positionally."""

import graphene
import pytest

from saleor.checkout.error_codes import CheckoutErrorCode
from saleor.graphql.tests.utils import get_graphql_content
from saleor.product.models import ProductChannelListing

MUTATION_CHECKOUT_LINES_UPDATE = """
    mutation checkoutLinesUpdate($id: ID, $lines: [CheckoutLineUpdateInput!]!) {
        checkoutLinesUpdate(id: $id, lines: $lines) {
            checkout { lines { id quantity } }
            errors { field code message }
        }
    }
"""


@pytest.mark.parametrize("removed_line_first", [True, False])
def test_r12_availability_validation_depends_on_input_order(
    removed_line_first, api_client, checkout_with_same_items_in_multiple_lines
):
    checkout = checkout_with_same_items_in_multiple_lines
    line_to_remove, line_to_grow = list(checkout.lines.all())
    assert line_to_remove.variant_id == line_to_grow.variant_id
    # product is no longer purchasable
    ProductChannelListing.objects.filter(
        product_id=line_to_grow.variant.product_id, channel_id=checkout.channel_id
    ).update(available_for_purchase_at=None)

    lines = [
        {
            "lineId": graphene.Node.to_global_id("CheckoutLine", line_to_remove.pk),
            "quantity": 0,
        },
        {
            "lineId": graphene.Node.to_global_id("CheckoutLine", line_to_grow.pk),
            "quantity": 5,
        },
    ]
    if not removed_line_first:
        lines.reverse()
    variables = {
        "id": graphene.Node.to_global_id("Checkout", checkout.pk),
        "lines": lines,
    }

    response = api_client.post_graphql(MUTATION_CHECKOUT_LINES_UPDATE, variables)
    data = get_graphql_content(response)["data"]["checkoutLinesUpdate"]

    if removed_line_first:
        # BUG: the unavailable variant's quantity is raised, validation skipped
        assert not data["errors"]
        line_to_grow.refresh_from_db()
        assert line_to_grow.quantity == 5
    else:
        # same request, other order: rejected
        assert len(data["errors"]) == 1
        assert (
            data["errors"][0]["code"]
            == CheckoutErrorCode.PRODUCT_UNAVAILABLE_FOR_PURCHASE.name
        )
