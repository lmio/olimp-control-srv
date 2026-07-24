# Toilet service API

`olimp-control-srv` is the source of truth for contestants, computers, class
membership, and physical classroom layouts. The toilet application owns its
proctor accounts independently; this API has no operator login endpoint.

The read-only endpoints below `/api/toilet/v1/` require
`CTRL_TOILET_AUTH_KEY`. This key must differ from `CTRL_AUTH_KEY`, which is
distributed to managed computers. In production, both keys must be distinct
random secrets of at least 32 UTF-8 bytes; sample and development values are
rejected at startup.

## Authentication

Requests carry:

- `X-LMIO-Toilet-Timestamp`: current Unix timestamp in seconds;
- `X-LMIO-Toilet-Auth`: lowercase hexadecimal HMAC-SHA256 over
  `timestamp + "\n" + METHOD + "\n" + request_full_path + "\n" + raw_body`.

Timestamps outside `CTRL_TOILET_AUTH_MAX_SKEW_SECONDS` are rejected. Responses
carry `X-LMIO-Toilet-Auth`, calculated over
`request_timestamp + "\n" + status_code + "\n" + raw_response_body`.

HMAC provides integrity and service authentication, not encryption. Production
traffic must use HTTPS or an equivalently private service network.

## Endpoints

### `GET /api/toilet/v1/students`

Returns a deterministic roster snapshot:

```json
{
  "students": [
    {"id": 12, "userid": "alice"}
  ]
}
```

`userid` is the durable identity shared with the independently imported CMS
roster. The numeric `id` is source metadata and must not be used as the
cross-service join key.

### `GET /api/toilet/v1/classes`

Returns the class catalog. `grid_cols` is an integer from 1 through 30 or
`null`.

```json
{
  "classes": [
    {
      "id": "2a82f6d8-c412-4580-a28a-79ba5646153b",
      "name": "101",
      "sequence_num": 1,
      "grid_cols": 6
    }
  ]
}
```

### `GET /api/toilet/v1/classes/{class_uuid}/layout`

Returns a live physical layout. `grid_row` and `grid_col` are integers from 1
through 30 or `null`; they are either both present or both absent, and occupied
coordinates are unique within a class. The assigned `student` is also live and
may be `null`.

```json
{
  "class": {
    "id": "2a82f6d8-c412-4580-a28a-79ba5646153b",
    "name": "101",
    "sequence_num": 1,
    "grid_cols": 6
  },
  "computers": [
    {
      "machine_id": "pc-01",
      "name": "PC 01",
      "sequence_num": 1,
      "grid_row": 1,
      "grid_col": 1,
      "student": {
        "id": 12,
        "userid": "alice"
      }
    }
  ]
}
```

### `POST /api/toilet/v1/student-assignment`

Input: `{"userid":"alice"}`. The response contains the current contestant
identifier, its one assigned computer (if any) with the live class and grid
position, and structured anomalies. Supported codes are `student_not_found`,
`no_computers`, and `computer_without_class`. Assignment anomalies return HTTP
200 so they remain distinguishable from service failures.

## CSV admin imports

Control contestants can be provisioned in Django admin from a CSV with exactly
one column:

```csv
identifier
alice
bob
```

Computers can be created or updated from the **Computers** admin page:

```csv
machine_id,name,class_id,sequence_num
pc-01,Computer 01,11111111-1111-1111-1111-111111111111,1
pc-spare,Spare computer,,2
```

`machine_id` is the idempotent lookup key. `class_id` is either blank or an
existing class UUID shown in the **Locations** admin page. Updating a computer
preserves its contestant mapping. Changing or clearing its class clears its
grid position; grid coordinates remain managed through the class layout UI.
Imports do not rename machine IDs or delete computers.

Current mappings are imported separately from the **Contestant-computer
mappings** admin page:

```csv
contestant_identifier,computer_name
alice,Computer 01
bob,Computer 02
```

Imports validate the whole file and are atomic. Each contestant and each
computer name may occur only once. A computer name must match exactly one
existing computer; unknown or ambiguous names are rejected. An exact existing
pair is idempotent; any other occupied contestant or computer is rejected with
an instruction to remove the current mapping first. Imports never replace or
remove mappings.

The legacy `import_students_csv` command remains available for identifier-only
contestant CSVs and supports `--dry-run`; mappings are managed separately.
The production migration creates the final contestant and mapping tables
directly because the deployed schema does not yet contain contestant records.
