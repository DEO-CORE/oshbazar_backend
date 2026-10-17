"""Lightweight request context (request id for logs)."""
import uuid

HEADER = "X-Request-ID"


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = request.META.get("HTTP_" + HEADER.replace("-", "_")) or uuid.uuid4().hex
        response = self.get_response(request)
        response[HEADER] = request.request_id
        return response
