I have many human review and AI review, with each review having multiple comments. For each pair of comments (one human and one AI), calculate a similarity score between 0.00 and 1.00, and return all similarity scores. Also sort human comments that are ranked by the similarity scores.

## Human review comments

{human_block}

## AI review comments

{ai_block}

---

## Required output format

Respond with exactly these two sections in order, and nothing else. **Use the exact comment IDs from the blocks above — do not abbreviate, rename, or renumber them.**

### Similarity scores

Emit ONE LINE per (human, AI) pair — every pair must appear exactly once, giving {pair_count} lines total. Format each line as:

    <human_id> | <ai_id> | <score>

where score is a float between 0.00 and 1.00 to two decimals. Example using IDs from the blocks above: `{example_human_id} | {example_ai_id} | 0.85`. No extra commentary between lines.

### Ranked human comments

Sort human comments by their best similarity to any AI comment, highest first. One line each, format:

    <rank>. <human_id> — best_match=<ai_id> sim=<score>

Example: `1. {example_human_id} — best_match={example_ai_id} sim=0.85`
