# Deficiency register CSV format

One row per deficiency. UTF-8, comma separated, header row required.

| column | required | meaning |
|---|---|---|
| `item_id` | yes | Unique ID as used in the field review report, e.g. `D-01`. |
| `location` | yes | Where the deficiency is, as written in the report. Level, room, gridline, elevation. |
| `description` | yes | What is deficient. |
| `evidence_required` | yes | What the engineer asked the contractor to provide. One or more slots separated by `;`. Each slot is `type: description`. |
| `review_date` | no | Date of the field review that raised the item, ISO `YYYY-MM-DD`. |
| `discipline` | no | Free text, e.g. `Fire protection`, `Structural`. |
| `reference_photo` | no | Path (relative to the CSV) to the photo the reviewer took when raising the item, JPEG or PNG. The agent compares contractor photos against it to establish location, and reads its EXIF GPS and altitude as the item's position. Must exist or the import fails. |

`type` in `evidence_required` must be one of `photo`, `report`, `letter`, `document`.
Each slot is checked independently. A deficiency is *evidence-complete* when every slot has at
least one confirmed, non-ambiguous piece of evidence linked to it. Evidence-complete does not mean
the work is acceptable; that is the engineer's decision.

Example:

```csv
item_id,location,description,evidence_required
D-03,"Roof, RTU-2 curb","Curb anchor bolts not installed","photo: installed anchors at all four corners; report: installer's anchor installation or torque report"
```
