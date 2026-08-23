# Corpus eval — asking questions whose answers are known

Every failure this project has hit has the same shape. A document is absent,
or present but unreadable, and the answer layer reports the gap as a fact
about the world:

> *"Which districts deny course credit after N unexcused absences?"*
> → **"Only Tarrytowns."**

That answer was true of the corpus and false of Westchester. Port Chester's
Policy 5100 says the same thing in the same words. The retrieval code was
correct, the synthesis was correct, the tests were green — and the answer was
wrong, because the corpus held 13 loose policy PDFs instead of a manual.

**Unit tests cannot catch that.** What catches it is asking the corpus a
question whose answer you already know, and checking the right passage comes
back.

    Actions -> eval -> Run workflow          # or: uv run herald-eval

## What is graded

**Retrieval, not prose.** If the passage reaches the answer layer, what the
model does with it is a separate concern with separate tests. Retrieval is
what has actually broken — every single time — and grading it is
deterministic, reproducible, and costs only the query embeddings. No
Anthropic key.

## The asymmetry that matters

`expect_present` is a **strong** claim: this district demonstrably says this,
and here is the string to prove it was retrieved.

`no_known_rule` is a **weak** one: a full manual was read and no such sentence
was found. That is *not* proof the rule does not exist — wording varies, and
we hold complete manuals for only some districts. So it is recorded for the
reader and **never graded as a required "no"**. Grading it would punish the
corpus for growing, and would bake this project's signature mistake — reading
absence as fact — into the very thing meant to detect it.

The one place absence *is* enforced is the **negative control**: a question
about a rule that exists nowhere. If any district returns evidence for it, the
case fails. A suite that only rewards recall teaches the system to
confabulate.

## Adding a case

Every `must_match` string must be **read out of a document we hold** — opened,
searched, copied. Never remembered, never guessed. A case built on a
half-remembered fact fails for the wrong reason and teaches you to ignore
failures.

```json
{
  "id": "short-slug",
  "question": "The question as a person would ask it",
  "kind": "coverage | norm | outlier | negative",
  "doc_type": "policy",
  "why": "What broke, or would break, if this stopped passing",
  "expect_present": [
    {"district": "tarrytowns",
     "must_match": ["18 unexcused", "will not receive credit"],
     "source": "5100 Student Attendance"}
  ]
}
```

Use **two or more** `must_match` strings where you can. One is often a false
pass: "18 unexcused" alone could come from a sentence about mailing a letter,
not about denying credit.

### `must_not_match` — the other half

Recall is a weak test once a corpus is large. Every district has a student
device policy *and* a staff cell-phone reimbursement policy, and they share
almost all their vocabulary. A question about students that returns the
reimbursement policy is well-retrieved, plausible, and wrong — and a suite
that only checks recall scores it as a pass.

```json
{"district": "white-plains",
 "must_match": ["sole grounds for the suspension"],
 "must_not_match": ["CELLPHONE REIMBURSEMENT"]}
```

Reach for this whenever the right and wrong answers are near neighbours in
embedding space. The report distinguishes the three outcomes — nothing came
back, the right document came back incomplete, the wrong document came back —
because they have different fixes.

## What each case is actually testing

Acquisition cases ("did the document survive") are only one failure mode, and
the one we have already fixed. The suite deliberately spans others:

| case | failure mode |
|---|---|
| `attendance-credit-threshold` | acquisition — the original policy gap |
| `dasa-attachment-only` | a policy whose entire text is an attachment |
| `identical-twin-policy` | content-hash dedupe dropping a real policy |
| `word-attachment-regulation` | a format PyMuPDF cannot open |
| `student-device-rule-norm` | **precision** — four different policy numbers for one mandate, against four staff policies that share the vocabulary |
| `vocabulary-mismatch-therapy-dogs` | **semantic recall** — "THERAPY DOGS" vs "Use of Assistance Animals". If this passes on FTS alone the embedding leg is doing nothing |
| `needle-in-a-long-document` | **chunking** — one sentence about ChatGPT inside a very long Code of Conduct |
| `regulation-not-the-policy` | **document choice** — the procedure is in the `-R`, not the policy it implements |
| `salary-grid-recovered` | OCR of a rotated grid (**expected to fail** until the re-OCR runs) |
| `negative-control-…` | confabulation |

## A failing case is not always a bug

`salary-grid-recovered` is expected to fail until the Tarrytowns vision-OCR
re-run happens. It names pending work instead of hiding it. When a case fails,
the report says which district returned nothing versus which returned the
wrong passage — those are different problems with different fixes.
