"""CSV helpers for report exports."""
import csv

from django.http import StreamingHttpResponse


class Echo:
    """Псевдо-файл для потокового csv.writer."""

    def write(self, value):
        return value


def csv_response(filename: str, header: list[str], rows) -> StreamingHttpResponse:
    writer = csv.writer(Echo())
    response = StreamingHttpResponse(
        _stream(writer, header, rows),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _stream(writer, header, rows):
    # UTF-8 BOM, чтобы Excel корректно открывал кириллицу.
    yield "\ufeff" + writer.writerow(header)
    for row in rows:
        yield writer.writerow(row)
