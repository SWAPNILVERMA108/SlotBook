from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """10 items per page by default.

    Clients may ask for ?page_size=25, but never more than 50, so nobody can request
    "everything" and slow the server down.
    """

    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50
