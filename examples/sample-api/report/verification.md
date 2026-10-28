# Verification

- mcp4api: 0.1.0
- source: openapi.yaml (`f675b645e7dcbcbb5c368ac9cda37338c57849e10fc14abc4dfe40f3e91edc96`)
- manifest: `1af5f2040ec5b3a416cdf8510e009a252a83f88d9c58398d9bcc27e811dd7983`
- model: claude-opus-5-5
- curation: agent
- evidence: fixtures
- allowlist: 127.0.0.1

15 tools generated · 14 verified · 1 known gaps · 0 skipped

## Tools

| Tool | Endpoints | Status | Evidence | Calls |
| --- | --- | --- | --- | --- |
| get_book | GET /books/{book_id}, GET /books/{book_id}/stats | known gap | fixtures | 3 |
| get_patron | GET /patrons/{patron_id} | verified | fixtures | 3 |
| list_copies | GET /books/{book_id}/copies | verified | fixtures | 3 |
| search_books | GET /books | verified | fixtures | 2 |
| add_copy | POST /books/{book_id}/copies | verified | fixtures | 3 |
| checkout_copy | POST /loans | verified | fixtures | 3 |
| create_book | POST /books | verified | fixtures | 3 |
| place_hold | POST /patrons/{patron_id}/holds | verified | fixtures | 3 |
| register_patron | POST /patrons | verified | fixtures | 3 |
| renew_loan | POST /loans/{loan_id}/renew | verified | fixtures | 3 |
| return_loan | POST /loans/{loan_id}/return | verified | fixtures | 3 |
| review_book | POST /books/{book_id}/reviews | verified | fixtures | 3 |
| update_book | PATCH /books/{book_id} | verified | fixtures | 3 |
| upload_book_file | POST /uploads, PUT /uploads/{upload_id}/parts/{part_number}, POST /uploads/{upload_id}/complete, PUT /books/{book_id}/file | verified | fixtures | 3 |
| delete_book | DELETE /books/{book_id} | verified | fixtures | 3 |

## Known gaps

### get_book

- call: `get_book:required`
- assertion: schema
- field: page_count
- expected: integer
- actual: string
- upstream status: 200

```
{"book_id": "book_001", "page_count": "320", "word_count": 84000}
```

### get_book

- call: `get_book:all`
- assertion: schema
- field: page_count
- expected: integer
- actual: string
- upstream status: 200

```
{"book_id": "book_001", "page_count": "320", "word_count": 84000}
```


## Excluded

- get_book

## Findings

None.

## Tokens

- input: 1600
- output: 320
- cache read: 0
- cache write: 0
- estimated USD: 0.0128
