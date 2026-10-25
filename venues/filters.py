import django_filters

from .models import Slot


class SlotFilter(django_filters.FilterSet):
    """Query-string filters for GET /api/slots/

    Examples:
      /api/slots/?city=ghaziabad
      /api/slots/?venue=3&date=2026-10-12
      /api/slots/?min_price=200&max_price=600
      /api/slots/?from_time=2026-10-12T17:00:00%2B05:30
    """

    venue = django_filters.NumberFilter(field_name="venue_id")
    city = django_filters.CharFilter(field_name="venue__city", lookup_expr="iexact")
    date = django_filters.DateFilter(field_name="start_time", lookup_expr="date")
    from_time = django_filters.IsoDateTimeFilter(field_name="start_time", lookup_expr="gte")
    to_time = django_filters.IsoDateTimeFilter(field_name="start_time", lookup_expr="lte")
    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="price", lookup_expr="lte")

    class Meta:
        model = Slot
        fields = []  # every filter is declared above
