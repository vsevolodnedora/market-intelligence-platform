You are generating a compact, machine-usable sidecar index for one Markdown document.

Goal:
Produce the smallest useful sidecar that lets another agent decide which sections of the document to read without loading the whole file.

Instructions:
- Read the entire document once.
- Do not rewrite or summarize the whole document.
- Extract only the structure and retrieval metadata needed for targeted loading.
- Be concise, precise, and deterministic.
- Prefer stable section IDs over line numbers.
- Use line ranges only as best-effort snapshot metadata, not as durable identifiers.
- Preserve the document’s actual hierarchy and ordering.
- Do not invent sections, dependencies, or tags that are not strongly supported by the text.
- If a field is unknown, omit it.

Output format:
Return valid YAML only.

Required top-level fields:
- doc_id: short stable slug for the document
- title: document title if present
- content_hash_hint: short hash-like fingerprint or version hint if derivable from content; otherwise omit
- sections: ordered list of section records

For each section record include:
- id: stable section ID derived from document identity + section meaning
- heading: exact section heading text
- level: markdown heading level as integer
- summary: one sentence describing what this section is for
- tags: short list of relevant tags such as module, phase, kind, topic
- depends_on: list of section IDs this section directly depends on, only if clearly implied
- line_start / line_end: only if easily available from the provided text

Rules for section IDs:
- IDs must be stable, readable, and unique within the document.
- Do not use raw line numbers in IDs.
- Prefer semantic IDs like DOC-3-5-ADMISSION or PLAN-U16-JOURNAL.

Optimization target:
- Minimize size while preserving retrieval usefulness.
- Include enough metadata for an agent to route follow-up reads to the right sections.
- Avoid verbose summaries, repeated tags, and redundant dependency links.

Quality bar:
- The sidecar should help an agent answer:
  1. Which sections matter for a given topic?
  2. Which sections must be read before another section?
  3. Which sections are contracts, invariants, flows, acceptance criteria, or style rules?

Now generate the YAML sidecar for the document below.

<Document>
{{DOCUMENT_TEXT}}
</Document>
