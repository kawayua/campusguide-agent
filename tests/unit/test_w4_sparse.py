from __future__ import annotations

import math

from campusguide.retrieval.sparse import BM25SparseEncoder, NullSparseEncoder, tokenize


def _dot(left, right) -> float:
    right_map = dict(zip(right.indices, right.values, strict=True))
    return sum(
        value * right_map.get(index, 0.0)
        for index, value in zip(left.indices, left.values, strict=False)
    )


def test_tokenize_cjk_unigram_bigram_and_ascii_runs() -> None:
    tokens = tokenize("推免学籍异动 2024 GPA")
    for expected in {"推", "免", "推免", "学", "籍", "学籍", "异", "动", "异动", "2024", "gpa"}:
        assert expected in tokens
    assert "2024gpa" not in tokens


def test_tokenize_empty_and_punctuation_only() -> None:
    assert tokenize("") == []
    assert tokenize("，。！？ \n\t") == []


def test_bm25_prefers_matching_document_and_is_deterministic() -> None:
    corpus = [
        "国家奖学金申请条件包括学习成绩排名前百分之十。",
        "国家助学贷款用于支付学费和住宿费。",
        "本科生转专业需要满足基本条件。",
    ]
    left = BM25SparseEncoder.fit(corpus)
    right = BM25SparseEncoder.fit(corpus)
    assert left.version == right.version

    docs = left.encode_documents(corpus)
    query = left.encode_query("国家奖学金申请条件")
    scores = [_dot(doc, query) for doc in docs]
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]
    assert scores[0] > 0


def test_query_is_idf_weighted_and_oov_is_empty() -> None:
    encoder = BM25SparseEncoder.fit(["奖学金", "奖学金", "助学贷款"])
    query = encoder.encode_query("奖学金")
    assert query.indices
    assert all(value > 0 for value in query.values)
    assert encoder.encode_query("火星殖民").indices == []


def test_empty_document_produces_empty_sparse_vector() -> None:
    encoder = BM25SparseEncoder.fit(["奖学金"])
    vector = encoder.encode_documents([""])[0]
    assert vector.indices == []
    assert vector.values == []


def test_save_load_round_trip(tmp_path) -> None:
    encoder = BM25SparseEncoder.fit(["奖学金申请", "助学贷款", "转专业"], k1=1.2, b=0.75)
    path = encoder.save(tmp_path / "sparse.json")
    loaded = BM25SparseEncoder.load(path)
    assert loaded.version == encoder.version
    assert loaded.index.idf_hash == encoder.index.idf_hash
    query = encoder.encode_query("奖学金")
    assert _dot(loaded.encode_documents(["奖学金申请"])[0], query) > 0


def test_idf_uses_bm25_formula() -> None:
    encoder = BM25SparseEncoder.fit(["奖学金", "奖学金", "助学贷款"])
    entries = dict(zip(encoder.index.tokens, encoder.index.idf, strict=True))
    expected = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))
    # “奖学”是双字 token，df=2；单字与双字共享同一 BM25 公式。
    assert math.isclose(entries["奖学"], expected, rel_tol=1e-9)
    assert math.isclose(entries["奖"], expected, rel_tol=1e-9)


def test_null_encoder_returns_empty_vectors() -> None:
    encoder = NullSparseEncoder()
    assert encoder.version == "null"
    assert encoder.encode_query("奖学金").indices == []
    assert encoder.encode_documents(["奖学金"])[0].indices == []
