# Licenses for external dataset samples

The files below are small samples copied from public datasets.  They are used
**for local evaluation only** in this repository: they are not redistributed as
a product, not used for training, and not intended to be a complete or
representative copy of the upstream data.  Each sample keeps its upstream
license; consult the source URL before any reuse beyond local testing.

| Directory | Upstream dataset | Source URL | License | Sample rows | Generated tasks |
|---|---|---|---|---:|---:|
| `mbpp/` | MBPP (sanitized), `mbpp/sanitized-mbpp.json` | https://github.com/google-research/google-research/tree/master/mbpp | CC-BY-4.0 (dataset); repository Apache-2.0 | 15 | 15 |
| `quixbugs/` | QuixBugs, `python_programs` + `correct_python_programs` + `json_testcases` | https://github.com/jkoppel/QuixBugs | MIT | 12 | 10 |
| `gsm8k/` | GSM8K, `grade_school_math/data/test.jsonl` | https://github.com/openai/grade-school-math | MIT | 15 | 15 |
| `wikitable/` | WikiTableQuestions (via Hugging Face `lighteval/wikitablequestions`, test split) | https://huggingface.co/datasets/lighteval/wikitablequestions | CC-BY-SA-4.0 | 15 | 15 |
| `hotpotqa/` | HotpotQA (distractor), validation split (via Hugging Face `hotpotqa/hotpot_qa`) | https://hotpotqa.github.io/ | CC-BY-SA-4.0 | 10 | 10 |

QuixBugs has 12 rows in the verified sample; the task brief asks for 10, so
`knapsack` and `kth` are present only in `quixbugs/_source/` and are not
converted into task directories.

## Source sample checksums

The exact sample files preserved under `<dataset>/_source/` have these SHA-256
digests:

```text
163ec2c6a9f54273091420cf1321f901fa8aaa91907d60d33c23b8cda7a94f70  mbpp/_source/mbpp_sample.jsonl
db2a6825221e4860a6d2d7a61c20b4d38332b47f170170cf751eed48a401773b  quixbugs/_source/quixbugs_sample.jsonl
a3bf4a45e7ceae3926d47e958f74faabb9698db2bfcb9232b5182a492cd5e3a9  gsm8k/_source/gsm8k_sample.jsonl
3bab32be83c95dc3ce4655f5d87644c0e249257f23d4fd0c05d623add9d5bdce  wikitable/_source/wtq_sample.jsonl
c0ddf880cbc6515a431ffb873a2c7237b4ee8887235d93e6cee84bfe4932d78b  hotpotqa/_source/hotpotqa_sample.jsonl
```

## Notes

* The `_source/` files are provenance copies and may contain upstream reference
  solutions (MBPP `code`, QuixBugs `correct_code`).  They sit outside every task
  `workspace/`; do not copy them into a task workspace.
* Generated task text is a transformation of the upstream question/instruction
  and remains subject to the upstream license.
* No sample was downloaded or refreshed by the harness at runtime.  The
  conversion script is network-free.
