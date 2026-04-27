"""Flask web UI for the paper review system.

Importing this package does NOT start the server nor load the reviewer
database — call `ai_paper_review.web.app.main()` or run `paper-review-web`
for that. The database load happens at import of `ai_paper_review.web.app`,
which keeps import of the parent `ai_paper_review.web` side-effect-free.
"""
