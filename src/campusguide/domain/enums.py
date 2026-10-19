"""领域枚举。"""

from __future__ import annotations

from enum import StrEnum


class Audience(StrEnum):
    UNDERGRADUATE = "undergraduate"
    GRADUATE = "graduate"
    ALL = "all"


class ArticleStatus(StrEnum):
    ACTIVE = "active"
    REPEALED = "repealed"
    DRAFT = "draft"
    UNKNOWN = "unknown"


class VersionMode(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    INCLUDE_REPEALED = "include_repealed"


class Intent(StrEnum):
    POLICY_QA = "policy_qa"
    PROCEDURE_GUIDANCE = "procedure_guidance"
    SMALLTALK = "smalltalk"
    OUT_OF_SCOPE = "out_of_scope"
    PRIVACY_REQUEST = "privacy_request"


class ClaimKind(StrEnum):
    ELIGIBILITY = "eligibility"
    STEP = "step"
    MATERIAL = "material"
    DEADLINE = "deadline"
    DEPARTMENT = "department"
    VERSION = "version"
    DEFINITION = "definition"
    OTHER = "other"


class VerificationDecision(StrEnum):
    PASS = "pass"
    REVISE = "revise"
    ABSTAIN = "abstain"
    CLARIFY = "clarify"


class DocType(StrEnum):
    REGULATION = "regulation"
    NOTICE = "notice"
    GUIDE = "guide"
    FAQ = "faq"
    OTHER = "other"


class BlockType(StrEnum):
    ARTICLE = "article"
    PREFACE = "preface"
    APPENDIX = "appendix"
    TABLE = "table"
    OTHER = "other"
