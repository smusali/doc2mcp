from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from typing import TypeVar

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER = "X-API-Key"
API_KEY = "shelf-sample-key"
HOST = "127.0.0.1"
CREATED_AT = "2024-01-15T00:00:00Z"
DUE_AT = "2024-02-15T00:00:00Z"
RENEWED_DUE_AT = "2024-03-15T00:00:00Z"
DEFAULT_LIMIT = 2
MAX_LIMIT = 100
_NEW_PAGE_COUNT = 100
_NEW_WORD_COUNT = 25000
_T = TypeVar("_T")


@dataclass
class Book:
    id: str
    title: str
    author: str
    page_count: int
    word_count: int
    created_at: str
    file_id: str | None


@dataclass
class Copy:
    id: str
    book_id: str
    barcode: str
    status: str
    created_at: str


@dataclass
class Review:
    id: str
    book_id: str
    patron_id: str
    rating: int
    body: str
    created_at: str


@dataclass
class Patron:
    id: str
    name: str
    email: str
    status: str
    created_at: str


@dataclass
class Hold:
    id: str
    book_id: str
    patron_id: str
    created_at: str


@dataclass
class Loan:
    id: str
    copy_id: str
    book_id: str
    patron_id: str
    status: str
    due_at: str
    renew_count: int
    created_at: str


@dataclass
class Upload:
    id: str
    filename: str
    content_type: str
    status: str
    part_count: int
    byte_count: int
    created_at: str
    parts: dict[int, int]


@dataclass
class AuditEvent:
    id: str
    action: str
    created_at: str


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if Headers(scope=scope).get(API_KEY_HEADER) != API_KEY:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class Store:
    def __init__(self) -> None:
        self.books: dict[str, Book] = {}
        self.copies: dict[str, Copy] = {}
        self.reviews: dict[str, Review] = {}
        self.patrons: dict[str, Patron] = {}
        self.holds: dict[str, Hold] = {}
        self.loans: dict[str, Loan] = {}
        self.uploads: dict[str, Upload] = {}
        self.audits: dict[str, AuditEvent] = {}
        self._counters = {
            "book": 4,
            "copy": 4,
            "review": 2,
            "patron": 3,
            "hold": 2,
            "loan": 2,
            "upload": 1,
        }
        self._seed()

    def _seed(self) -> None:
        self.books = {
            "book_001": Book("book_001", "North Wind", "Ada North", 320, 84000, CREATED_AT, None),
            "book_002": Book("book_002", "South Dock", "Lin Park", 210, 55000, CREATED_AT, None),
            "book_003": Book("book_003", "East Archive", "Omar West", 128, 30000, CREATED_AT, None),
        }
        self.copies = {
            "copy_001": Copy("copy_001", "book_001", "C-001", "on_loan", CREATED_AT),
            "copy_002": Copy("copy_002", "book_001", "C-002", "available", CREATED_AT),
            "copy_003": Copy("copy_003", "book_002", "C-003", "available", CREATED_AT),
        }
        self.patrons = {
            "patron_001": Patron(
                "patron_001", "Riley Chen", "riley@example.test", "active", CREATED_AT
            ),
            "patron_002": Patron(
                "patron_002", "Sam Okonkwo", "sam@example.test", "active", CREATED_AT
            ),
        }
        self.reviews = {
            "review_001": Review(
                "review_001", "book_001", "patron_001", 5, "Clear and short.", CREATED_AT
            ),
        }
        self.holds = {"hold_001": Hold("hold_001", "book_003", "patron_002", CREATED_AT)}
        self.loans = {
            "loan_001": Loan(
                "loan_001",
                "copy_001",
                "book_001",
                "patron_001",
                "active",
                DUE_AT,
                0,
                CREATED_AT,
            ),
        }
        self.audits = {"audit_001": AuditEvent("audit_001", "seed", CREATED_AT)}

    def _mint(self, prefix: str) -> str:
        number = self._counters[prefix]
        self._counters[prefix] = number + 1
        return f"{prefix}_{number:03d}"

    def _sorted(self, items: dict[str, _T]) -> list[_T]:
        return [items[key] for key in sorted(items, key=_id_key)]

    def _book(self, book_id: str) -> Book:
        return _missing(book_id, self.books)

    def _copy(self, copy_id: str) -> Copy:
        return _missing(copy_id, self.copies)

    def _review(self, review_id: str) -> Review:
        return _missing(review_id, self.reviews)

    def _patron(self, patron_id: str) -> Patron:
        return _missing(patron_id, self.patrons)

    def _hold(self, hold_id: str) -> Hold:
        return _missing(hold_id, self.holds)

    def _loan(self, loan_id: str) -> Loan:
        return _missing(loan_id, self.loans)

    def _upload(self, upload_id: str) -> Upload:
        return _missing(upload_id, self.uploads)

    async def list_books(self, request: Request) -> JSONResponse:
        rows = [_book_json(book) for book in self._sorted(self.books)]
        return _page(rows, _cursor(request), _limit(request))

    async def create_book(self, request: Request) -> JSONResponse:
        body = await _body(request)
        _keys(body, frozenset({"title", "author"}))
        book = Book(
            id=self._mint("book"),
            title=_required_string(body, "title"),
            author=_required_string(body, "author"),
            page_count=_NEW_PAGE_COUNT,
            word_count=_NEW_WORD_COUNT,
            created_at=CREATED_AT,
            file_id=None,
        )
        self.books[book.id] = book
        return JSONResponse(_book_json(book), status_code=201)

    async def get_book(self, request: Request) -> JSONResponse:
        return JSONResponse(_book_json(self._book(_path(request, "book_id"))))

    async def update_book(self, request: Request) -> JSONResponse:
        book = self._book(_path(request, "book_id"))
        body = await _body(request)
        allowed = frozenset({"title", "author"})
        _keys(body, allowed)
        _require_any(body, allowed)
        title = _optional_string(body, "title")
        author = _optional_string(body, "author")
        if title is not None:
            book.title = title
        if author is not None:
            book.author = author
        return JSONResponse(_book_json(book))

    async def delete_book(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        if _uses_book(self, book_id):
            raise HTTPException(status_code=400, detail="book is in use")
        del self.books[book_id]
        return _deletion(book_id)

    async def get_book_stats(self, request: Request) -> JSONResponse:
        return JSONResponse(_stats_json(self._book(_path(request, "book_id"))))

    async def get_book_page_count(self, request: Request) -> JSONResponse:
        return JSONResponse(_page_count_json(self._book(_path(request, "book_id"))))

    async def list_copies(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        rows = [_copy_json(copy) for copy in self._sorted(self.copies) if copy.book_id == book_id]
        return _page(rows, _cursor(request), _limit(request))

    async def create_copy(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        body = await _body(request)
        _keys(body, frozenset({"barcode"}))
        copy = Copy(
            id=self._mint("copy"),
            book_id=book_id,
            barcode=_required_string(body, "barcode"),
            status="available",
            created_at=CREATED_AT,
        )
        self.copies[copy.id] = copy
        return JSONResponse(_copy_json(copy), status_code=201)

    async def get_copy(self, request: Request) -> JSONResponse:
        return JSONResponse(_copy_json(self._copy(_path(request, "copy_id"))))

    async def update_copy(self, request: Request) -> JSONResponse:
        copy = self._copy(_path(request, "copy_id"))
        body = await _body(request)
        _keys(body, frozenset({"barcode"}))
        _require_any(body, frozenset({"barcode"}))
        barcode = _optional_string(body, "barcode")
        if barcode is not None:
            copy.barcode = barcode
        return JSONResponse(_copy_json(copy))

    async def delete_copy(self, request: Request) -> JSONResponse:
        copy = self._copy(_path(request, "copy_id"))
        if copy.status != "available" or _loan_for_copy(self, copy.id):
            raise HTTPException(status_code=400, detail="copy is on loan")
        del self.copies[copy.id]
        return _deletion(copy.id)

    async def attach_book_file(self, request: Request) -> JSONResponse:
        book = self._book(_path(request, "book_id"))
        body = await _body(request)
        _keys(body, frozenset({"upload_id"}))
        upload = self._upload(_required_string(body, "upload_id"))
        if upload.status != "complete":
            raise HTTPException(status_code=400, detail="upload is incomplete")
        book.file_id = upload.id
        return JSONResponse({"book_id": book.id, "file_id": upload.id})

    async def list_reviews(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        rows = [
            _review_json(review)
            for review in self._sorted(self.reviews)
            if review.book_id == book_id
        ]
        return _page(rows, _cursor(request), _limit(request))

    async def create_review(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        body = await _body(request)
        _keys(body, frozenset({"patron_id", "rating", "body"}))
        patron_id = _required_string(body, "patron_id")
        self._patron(patron_id)
        review = Review(
            id=self._mint("review"),
            book_id=book_id,
            patron_id=patron_id,
            rating=_required_int(body, "rating", minimum=1, maximum=5),
            body=_required_string(body, "body"),
            created_at=CREATED_AT,
        )
        self.reviews[review.id] = review
        return JSONResponse(_review_json(review), status_code=201)

    async def get_review(self, request: Request) -> JSONResponse:
        return JSONResponse(_review_json(self._review(_path(request, "review_id"))))

    async def update_review(self, request: Request) -> JSONResponse:
        review = self._review(_path(request, "review_id"))
        body = await _body(request)
        allowed = frozenset({"rating", "body"})
        _keys(body, allowed)
        _require_any(body, allowed)
        rating = _optional_int(body, "rating", minimum=1, maximum=5)
        text = _optional_string(body, "body")
        if rating is not None:
            review.rating = rating
        if text is not None:
            review.body = text
        return JSONResponse(_review_json(review))

    async def delete_review(self, request: Request) -> JSONResponse:
        review_id = _path(request, "review_id")
        self._review(review_id)
        del self.reviews[review_id]
        return _deletion(review_id)

    async def list_patrons(self, request: Request) -> JSONResponse:
        rows = [_patron_json(patron) for patron in self._sorted(self.patrons)]
        return _page(rows, _cursor(request), _limit(request))

    async def create_patron(self, request: Request) -> JSONResponse:
        body = await _body(request)
        _keys(body, frozenset({"name", "email"}))
        patron = Patron(
            id=self._mint("patron"),
            name=_required_string(body, "name"),
            email=_required_string(body, "email"),
            status="active",
            created_at=CREATED_AT,
        )
        self.patrons[patron.id] = patron
        return JSONResponse(_patron_json(patron), status_code=201)

    async def get_patron(self, request: Request) -> JSONResponse:
        return JSONResponse(_patron_json(self._patron(_path(request, "patron_id"))))

    async def update_patron(self, request: Request) -> JSONResponse:
        patron = self._patron(_path(request, "patron_id"))
        body = await _body(request)
        allowed = frozenset({"name", "email"})
        _keys(body, allowed)
        _require_any(body, allowed)
        name = _optional_string(body, "name")
        email = _optional_string(body, "email")
        if name is not None:
            patron.name = name
        if email is not None:
            patron.email = email
        return JSONResponse(_patron_json(patron))

    async def delete_patron(self, request: Request) -> JSONResponse:
        patron_id = _path(request, "patron_id")
        self._patron(patron_id)
        if _uses_patron(self, patron_id):
            raise HTTPException(status_code=400, detail="patron is in use")
        del self.patrons[patron_id]
        return _deletion(patron_id)

    async def list_holds(self, request: Request) -> JSONResponse:
        patron_id = _path(request, "patron_id")
        self._patron(patron_id)
        rows = [
            _hold_json(hold) for hold in self._sorted(self.holds) if hold.patron_id == patron_id
        ]
        return _page(rows, _cursor(request), _limit(request))

    async def create_hold(self, request: Request) -> JSONResponse:
        patron = self._patron(_path(request, "patron_id"))
        if patron.status != "active":
            raise HTTPException(status_code=400, detail="patron is suspended")
        body = await _body(request)
        _keys(body, frozenset({"book_id"}))
        book = self._book(_required_string(body, "book_id"))
        hold = Hold(
            id=self._mint("hold"),
            book_id=book.id,
            patron_id=patron.id,
            created_at=CREATED_AT,
        )
        self.holds[hold.id] = hold
        return JSONResponse(_hold_json(hold), status_code=201)

    async def delete_hold(self, request: Request) -> JSONResponse:
        hold_id = _path(request, "hold_id")
        self._hold(hold_id)
        del self.holds[hold_id]
        return _deletion(hold_id)

    async def list_loans(self, request: Request) -> JSONResponse:
        rows = [_loan_json(loan) for loan in self._sorted(self.loans)]
        return _page(rows, _cursor(request), _limit(request))

    async def create_loan(self, request: Request) -> JSONResponse:
        body = await _body(request)
        _keys(body, frozenset({"copy_id", "patron_id"}))
        copy = self._copy(_required_string(body, "copy_id"))
        patron = self._patron(_required_string(body, "patron_id"))
        if copy.status != "available":
            raise HTTPException(status_code=400, detail="copy is not available")
        if patron.status != "active":
            raise HTTPException(status_code=400, detail="patron is suspended")
        loan = Loan(
            id=self._mint("loan"),
            copy_id=copy.id,
            book_id=copy.book_id,
            patron_id=patron.id,
            status="active",
            due_at=DUE_AT,
            renew_count=0,
            created_at=CREATED_AT,
        )
        copy.status = "on_loan"
        self.loans[loan.id] = loan
        return JSONResponse(_loan_json(loan), status_code=201)

    async def get_loan(self, request: Request) -> JSONResponse:
        return JSONResponse(_loan_json(self._loan(_path(request, "loan_id"))))

    async def return_loan(self, request: Request) -> JSONResponse:
        loan = self._loan(_path(request, "loan_id"))
        if loan.status != "active":
            raise HTTPException(status_code=400, detail="loan is not active")
        loan.status = "returned"
        self._copy(loan.copy_id).status = "available"
        return JSONResponse(_loan_json(loan))

    async def renew_loan(self, request: Request) -> JSONResponse:
        loan = self._loan(_path(request, "loan_id"))
        if loan.status != "active":
            raise HTTPException(status_code=400, detail="loan is not active")
        loan.renew_count += 1
        loan.due_at = RENEWED_DUE_AT
        return JSONResponse(_loan_json(loan))

    async def list_book_loans(self, request: Request) -> JSONResponse:
        book_id = _path(request, "book_id")
        self._book(book_id)
        rows = [_loan_json(loan) for loan in self._sorted(self.loans) if loan.book_id == book_id]
        return _page(rows, _cursor(request), _limit(request))

    async def list_patron_loans(self, request: Request) -> JSONResponse:
        patron_id = _path(request, "patron_id")
        self._patron(patron_id)
        rows = [
            _loan_json(loan) for loan in self._sorted(self.loans) if loan.patron_id == patron_id
        ]
        return _page(rows, _cursor(request), _limit(request))

    async def create_upload(self, request: Request) -> JSONResponse:
        body = await _body(request)
        _keys(body, frozenset({"filename", "content_type"}))
        upload = Upload(
            id=self._mint("upload"),
            filename=_required_string(body, "filename"),
            content_type=_required_string(body, "content_type"),
            status="open",
            part_count=0,
            byte_count=0,
            created_at=CREATED_AT,
            parts={},
        )
        self.uploads[upload.id] = upload
        return JSONResponse(_upload_json(upload), status_code=201)

    async def upload_part(self, request: Request) -> JSONResponse:
        upload = self._upload(_path(request, "upload_id"))
        if upload.status != "open":
            raise HTTPException(status_code=400, detail="upload is closed")
        number = _part_number(request)
        body = await _body(request)
        _keys(body, frozenset({"data"}))
        size = _decoded_size(_required_string(body, "data"))
        upload.parts[number] = size
        _retotal(upload)
        return JSONResponse({"upload_id": upload.id, "part_number": number, "size": size})

    async def complete_upload(self, request: Request) -> JSONResponse:
        upload = self._upload(_path(request, "upload_id"))
        if upload.status != "complete":
            if not upload.parts:
                raise HTTPException(status_code=400, detail="upload has no parts")
            upload.status = "complete"
            _retotal(upload)
        return JSONResponse(_upload_json(upload))

    async def list_audit_events(self, request: Request) -> JSONResponse:
        rows = [_audit_json(event) for event in self._sorted(self.audits)]
        return _page(rows, _cursor(request), _limit(request))

    async def suspend_patron(self, request: Request) -> JSONResponse:
        patron = self._patron(_path(request, "patron_id"))
        patron.status = "suspended"
        return JSONResponse(_patron_json(patron))

    async def reinstate_patron(self, request: Request) -> JSONResponse:
        patron = self._patron(_path(request, "patron_id"))
        patron.status = "active"
        return JSONResponse(_patron_json(patron))

    async def get_circulation(self, _request: Request) -> JSONResponse:
        active = sum(1 for loan in self.loans.values() if loan.status == "active")
        return JSONResponse({"active_loans": active, "open_holds": len(self.holds)})


def create_app() -> Starlette:
    store = Store()
    return Starlette(
        routes=[
            Route("/books", store.list_books, methods=["GET"]),
            Route("/books", store.create_book, methods=["POST"]),
            Route("/books/{book_id}", store.get_book, methods=["GET"]),
            Route("/books/{book_id}", store.update_book, methods=["PATCH"]),
            Route("/books/{book_id}", store.delete_book, methods=["DELETE"]),
            Route("/books/{book_id}/stats", store.get_book_stats, methods=["GET"]),
            Route("/books/{book_id}/page-count", store.get_book_page_count, methods=["GET"]),
            Route("/books/{book_id}/copies", store.list_copies, methods=["GET"]),
            Route("/books/{book_id}/copies", store.create_copy, methods=["POST"]),
            Route("/books/{book_id}/file", store.attach_book_file, methods=["PUT"]),
            Route("/books/{book_id}/reviews", store.list_reviews, methods=["GET"]),
            Route("/books/{book_id}/reviews", store.create_review, methods=["POST"]),
            Route("/books/{book_id}/loans", store.list_book_loans, methods=["GET"]),
            Route("/copies/{copy_id}", store.get_copy, methods=["GET"]),
            Route("/copies/{copy_id}", store.update_copy, methods=["PATCH"]),
            Route("/copies/{copy_id}", store.delete_copy, methods=["DELETE"]),
            Route("/reviews/{review_id}", store.get_review, methods=["GET"]),
            Route("/reviews/{review_id}", store.update_review, methods=["PATCH"]),
            Route("/reviews/{review_id}", store.delete_review, methods=["DELETE"]),
            Route("/patrons", store.list_patrons, methods=["GET"]),
            Route("/patrons", store.create_patron, methods=["POST"]),
            Route("/patrons/{patron_id}", store.get_patron, methods=["GET"]),
            Route("/patrons/{patron_id}", store.update_patron, methods=["PATCH"]),
            Route("/patrons/{patron_id}", store.delete_patron, methods=["DELETE"]),
            Route("/patrons/{patron_id}/holds", store.list_holds, methods=["GET"]),
            Route("/patrons/{patron_id}/holds", store.create_hold, methods=["POST"]),
            Route("/patrons/{patron_id}/loans", store.list_patron_loans, methods=["GET"]),
            Route("/holds/{hold_id}", store.delete_hold, methods=["DELETE"]),
            Route("/loans", store.list_loans, methods=["GET"]),
            Route("/loans", store.create_loan, methods=["POST"]),
            Route("/loans/{loan_id}", store.get_loan, methods=["GET"]),
            Route("/loans/{loan_id}/return", store.return_loan, methods=["POST"]),
            Route("/loans/{loan_id}/renew", store.renew_loan, methods=["POST"]),
            Route("/uploads", store.create_upload, methods=["POST"]),
            Route(
                "/uploads/{upload_id}/parts/{part_number}",
                store.upload_part,
                methods=["PUT"],
            ),
            Route("/uploads/{upload_id}/complete", store.complete_upload, methods=["POST"]),
            Route("/admin/audit-events", store.list_audit_events, methods=["GET"]),
            Route(
                "/admin/patrons/{patron_id}/suspend",
                store.suspend_patron,
                methods=["POST"],
            ),
            Route(
                "/admin/patrons/{patron_id}/reinstate",
                store.reinstate_patron,
                methods=["POST"],
            ),
            Route("/admin/circulation", store.get_circulation, methods=["GET"]),
        ],
        middleware=[Middleware(ApiKeyMiddleware)],
        exception_handlers={HTTPException: _http_error},
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0].isdigit():
        print("usage: server.py PORT", file=sys.stderr)
        return 2
    port = int(args[0])
    if port < 1 or port > 65535:
        print("usage: server.py PORT", file=sys.stderr)
        return 2
    uvicorn.run(create_app(), host=HOST, port=port)
    return 0


def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse({"error": detail}, status_code=exc.status_code)


def _id_key(resource_id: str) -> tuple[str, int]:
    prefix, separator, number = resource_id.rpartition("_")
    if separator and number.isdigit():
        return prefix, int(number)
    return resource_id, 0


def _missing(resource_id: str, items: dict[str, _T]) -> _T:
    found = items.get(resource_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    return found


def _path(request: Request, name: str) -> str:
    value: object = request.path_params[name]
    if not isinstance(value, str) or value == "":
        raise HTTPException(status_code=404, detail="not found")
    return value


def _cursor(request: Request) -> str | None:
    raw = request.query_params.get("cursor")
    if raw is None or raw == "":
        return None
    return raw


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    return _parse_int(raw, minimum=1, maximum=MAX_LIMIT, detail="invalid limit")


def _part_number(request: Request) -> int:
    return _parse_int(
        _path(request, "part_number"),
        minimum=1,
        maximum=None,
        detail="invalid part_number",
    )


def _parse_int(raw: str, *, minimum: int, maximum: int | None, detail: str) -> int:
    if not raw.isdigit():
        raise HTTPException(status_code=400, detail=detail)
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise HTTPException(status_code=400, detail=detail)
    return value


async def _body(request: Request) -> dict[str, object]:
    try:
        payload: object = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    body: dict[str, object] = {}
    for key, item in payload.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="body must be an object")
        body[key] = item
    return body


def _keys(body: dict[str, object], allowed: frozenset[str]) -> None:
    if any(key not in allowed for key in body):
        raise HTTPException(status_code=400, detail="unknown field")


def _require_any(body: dict[str, object], names: frozenset[str]) -> None:
    if not any(name in body for name in names):
        raise HTTPException(status_code=400, detail="empty patch")


def _required_string(body: dict[str, object], name: str) -> str:
    if name not in body:
        raise HTTPException(status_code=400, detail=f"missing {name}")
    value = body[name]
    if not isinstance(value, str) or value == "":
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return value


def _optional_string(body: dict[str, object], name: str) -> str | None:
    if name not in body:
        return None
    return _required_string(body, name)


def _required_int(
    body: dict[str, object], name: str, *, minimum: int, maximum: int | None = None
) -> int:
    if name not in body:
        raise HTTPException(status_code=400, detail=f"missing {name}")
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    if value < minimum or (maximum is not None and value > maximum):
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return value


def _optional_int(
    body: dict[str, object], name: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    if name not in body:
        return None
    return _required_int(body, name, minimum=minimum, maximum=maximum)


def _decoded_size(data: str) -> int:
    try:
        return len(base64.b64decode(data, validate=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid data") from exc


def _retotal(upload: Upload) -> None:
    upload.part_count = len(upload.parts)
    upload.byte_count = sum(upload.parts.values())


def _deletion(resource_id: str) -> JSONResponse:
    return JSONResponse({"id": resource_id, "deleted": True})


def _page(rows: list[dict[str, object]], cursor: str | None, limit: int) -> JSONResponse:
    start = 0
    if cursor is not None:
        found: int | None = None
        for index, row in enumerate(rows):
            if row.get("id") == cursor:
                found = index + 1
                break
        if found is None:
            raise HTTPException(status_code=400, detail="unknown cursor")
        start = found
    window = rows[start : start + limit]
    has_more = start + limit < len(rows)
    next_cursor: str | None = None
    if has_more:
        last = window[-1]["id"]
        if not isinstance(last, str):
            raise HTTPException(status_code=400, detail="unknown cursor")
        next_cursor = last
    return JSONResponse({"data": window, "has_more": has_more, "next_cursor": next_cursor})


def _uses_book(store: Store, book_id: str) -> bool:
    if any(copy.book_id == book_id for copy in store.copies.values()):
        return True
    if any(review.book_id == book_id for review in store.reviews.values()):
        return True
    if any(hold.book_id == book_id for hold in store.holds.values()):
        return True
    return any(loan.book_id == book_id for loan in store.loans.values())


def _uses_patron(store: Store, patron_id: str) -> bool:
    if any(review.patron_id == patron_id for review in store.reviews.values()):
        return True
    if any(hold.patron_id == patron_id for hold in store.holds.values()):
        return True
    return any(loan.patron_id == patron_id for loan in store.loans.values())


def _loan_for_copy(store: Store, copy_id: str) -> bool:
    return any(loan.copy_id == copy_id for loan in store.loans.values())


def _book_json(book: Book) -> dict[str, object]:
    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "created_at": book.created_at,
        "file_id": book.file_id,
    }


def _stats_json(book: Book) -> dict[str, object]:
    return {
        "book_id": book.id,
        "page_count": str(book.page_count),
        "word_count": book.word_count,
    }


def _page_count_json(book: Book) -> dict[str, object]:
    return {"book_id": book.id, "page_count": book.page_count}


def _copy_json(copy: Copy) -> dict[str, object]:
    return {
        "id": copy.id,
        "book_id": copy.book_id,
        "barcode": copy.barcode,
        "status": copy.status,
        "created_at": copy.created_at,
    }


def _review_json(review: Review) -> dict[str, object]:
    return {
        "id": review.id,
        "book_id": review.book_id,
        "patron_id": review.patron_id,
        "rating": review.rating,
        "body": review.body,
        "created_at": review.created_at,
    }


def _patron_json(patron: Patron) -> dict[str, object]:
    return {
        "id": patron.id,
        "name": patron.name,
        "email": patron.email,
        "status": patron.status,
        "created_at": patron.created_at,
    }


def _hold_json(hold: Hold) -> dict[str, object]:
    return {
        "id": hold.id,
        "book_id": hold.book_id,
        "patron_id": hold.patron_id,
        "created_at": hold.created_at,
    }


def _loan_json(loan: Loan) -> dict[str, object]:
    return {
        "id": loan.id,
        "copy_id": loan.copy_id,
        "book_id": loan.book_id,
        "patron_id": loan.patron_id,
        "status": loan.status,
        "due_at": loan.due_at,
        "renew_count": loan.renew_count,
        "created_at": loan.created_at,
    }


def _upload_json(upload: Upload) -> dict[str, object]:
    return {
        "id": upload.id,
        "filename": upload.filename,
        "content_type": upload.content_type,
        "status": upload.status,
        "part_count": upload.part_count,
        "byte_count": upload.byte_count,
        "created_at": upload.created_at,
    }


def _audit_json(event: AuditEvent) -> dict[str, object]:
    return {"id": event.id, "action": event.action, "created_at": event.created_at}


if __name__ == "__main__":
    raise SystemExit(main())
