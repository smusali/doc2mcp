# Verification

- doc2mcp: 0.1.0
- source: openapi.yaml (`10b6233ec3ce6f2e8b0b0d8248836ce1e78986f3e6fffc5b362f228c12e8f5e2`)
- manifest: `a2d48b93c0802583fea849310d3a4543bf3e96b682fce70a7512fccac9703874`
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
