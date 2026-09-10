# Source note: the office's "Final Occupancy Documents Checklist"

Read 2026-09-11 from an Excel tracker the office QA team circulated by email (Aug 25 and again
Sep 3 2026). One project's copy; the address, the people and the trade companies are left out here.

## What it is

Not a field-review checklist. It is the **closeout paperwork list**: every document the
Coordinating Registered Professional (CRP) must collect from the consultants and trades before
the city will issue final occupancy. One row per document, tracked through a fixed set of states.

## Columns in the tracker

| Column | Meaning |
|---|---|
| Required Doc by CRP | tick if the CRP needs it |
| Required Doc by Eng | tick if the engineer's office needs it |
| Sr.No / Document | the document name |
| Agency | who produces it (consultant or trade, per project) |
| Requested by Eng → Sent by Builder → Received → Reviewed → Rejected / Accepted | the state chain, one tick each |
| Attached Doc link | where the file is |
| Status / Remarks | free text ("Complete", "follow up", "work related") |

## The 37 documents, grouped

**Builder's own items (CP's list)**
1. Occupancy Permit Application
2. Demonstration Test Protocol
3. Fire Safety Plan
4. Non-Encroachment Survey
5. Confirmation of Development Permit Compliance (architect)

**Letters of Assurance, Schedule C-A / C-B, one per discipline**
6. Schedule C-A and C-B (CRP)
7. C-B Civil · 8. C-B Geotechnical · 9. C-B Structural · 10. C-B Mechanical
11. C-B Electrical · 12. C-B Plumbing · 13. C-B Fire Protection (sprinkler)

**Schedule B, Electrical, with its sub-items**
- Fire Alarm Verification Certificate and Report
- Appendix C of CAN/ULC-S537
- ULC Certificate for Monitoring Station, site specific
- CAN/ULC-S1001 Certificate
- Confirmation of Bi-directional Amplification design and installation

**Schedule S-B (supporting, during construction)**
14. Envelope · 15. Windows · 16. Guards · 17. Mechanical seismic · 18. Plumbing seismic
19. Electrical seismic · 20. Fire sprinkler seismic

**Schedule S-C (supporting, at completion)**
21. Envelope · 22. Windows · 23. Guards · 24. Mechanical seismic · 25. Plumbing seismic
26. Electrical seismic · 27. Fire sprinkler seismic

**Trade certificates and reports**
28. Sprinkler material test certificate, underground piping
29. Sprinkler material test certificate, above ground piping
30. Standpipe material test certificate, underground (NFPA 14 form)
31. Standpipe material test certificate, above ground (NFPA 14 form)
32. Backflow preventer test report
33. Chlorination certificate
34. Heat trace confirmation letter
35. Parkade CO detector calibration certificate
36. HVAC balancing report (life-safety fans)
37. Fire Alarm Verification Certificate and Report

## What the office itself has not defined yet

The Sep 3 email from the office QA team asks the principal for exactly the things an app would
need: the document list per discipline, when each document becomes required, who requests and
who provides it, what "received", "reviewed", "rejected", "revised" and "accepted" mean, and the
conditional cases. Until those answers exist, any state machine in the app is a guess.

## How it relates to Closeout

- It does **not** feed the review-start to-do list (that needs the per-discipline field checklists,
  see [[vcanmanage_discipline_sources]]).
- It **does** map onto the project closeout idea in `02-plan.md`: a "Documents before occupancy"
  card per discipline, with the state chain above and a link to the file in the project folder.
  Deterministic code, no agent needed for the list; the agent could only help by reading a
  received certificate and checking it names the right project and discipline.
- Not for the hackathon build. Rule from `README.md` applies: nothing enters the app before the
  entry is submitted.
