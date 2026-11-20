---
name: research
description: How to answer a question from the document corpus with citations. Use for "how does X work", "what does the standard say", "what is the limit of", literature and design questions.
---

# Answering from documents

1. A quick fact (one value, one definition): call search_knowledge, answer in a sentence, cite it.
2. Anything that needs reading or combining several documents: delegate to the librarian subagent with the
   question as asked plus any context from the conversation (the object, product, version or standard it is about).
3. A question that mixes live state and documents ("is this link's measured value within what the standard
   allows?"): read the live state with the tools (or the subagent that owns it), ask the librarian for the rule,
   then combine the two and say which part came from where.
4. Every claim taken from a document carries its citation [source p.N]. If the corpus does not hold it, say so.
