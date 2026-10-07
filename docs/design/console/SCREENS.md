# Screens, phase 1

See `README.md` for the board-to-PRD map and `PRD-phase1.md` for what each board's elements do in phase 1. This file keeps only the case-page interaction contract rows that phase 1 implements; the rest of the v7 contract is phase 2.

## Case page · interaction contract, phase 1

| Trigger | What happens | Recorded as |
|---|---|---|
| Approve on the decision block (reversible) | Today's approve endpoint; undo fuse 8 s | Approval record, as today |
| Hold to approve (cannot be undone) | 1.6 s hold, then today's approve endpoint; no undo | Approval record, as today |
| Reject → reason (free text in phase 1) | Today's reject endpoint | Approval record, as today |
| Ask | Answer from today's chat with the case attached; citation chips open Evidence at the row when a row id is cited | Conversation, private to the user (PRD D4) |
| Why? (Record row) | Opens the composer in Ask with that row attached | Not recorded |
| Reopen (closed case) | Today's reopen; verdict withdrawn | Case audit row, as today |
| Replay, Verify chain, Export audit (Record tab) | Today's run replay, ledger verify and export | Not recorded |

Everything else in the v7 contract (Approve narrower, Hold with reason and duration, Revert, Stop before this, tier changes, Tell and Do, evidence row marks, on-demand work, Agree/Disagree) is phase 2 and is Omitted or Later per the PRD.
