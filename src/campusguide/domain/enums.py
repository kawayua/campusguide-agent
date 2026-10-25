"""领域枚举。"""

from __future__ import annotations

from enum import StrEnum


class Audience(StrEnum):
    UNDERGRADUATE = "undergraduate"
    GRADUATE = "graduate"
    GRADUATE_MASTER = "graduate_master"
    GRADUATE_DOCTOR = "graduate_doctor"
    FACULTY = "faculty"
    STAFF = "staff"
    UNIT = "unit"
    PUBLIC = "public"
    POSTDOC = "postdoc"
    ALL = "all"


class AudienceStage(StrEnum):
    YEAR1 = "year1"
    YEAR2 = "year2"
    YEAR3 = "year3"
    YEAR4 = "year4"
    YEAR5 = "year5"
    YEAR6 = "year6"
    FINAL_YEAR = "final_year"


class StudentStatus(StrEnum):
    ENROLLED = "enrolled"
    GRADUATING = "graduating"
    SUSPENDED = "suspended"
    COMPLETED = "completed"


class ProgramType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    ACADEMIC = "academic"
    PROFESSIONAL = "professional"


class StudentGroup(StrEnum):
    FINANCIAL_AID = "financial_aid"
    DIRECTED = "directed"
    NON_DIRECTED = "non_directed"
    TOP_UP = "top_up"
    SECOND_DEGREE = "second_degree"
    INTERNATIONAL = "international"
    HK_MO_TW = "hk_mo_tw"
    VETERAN = "veteran"
    MINORITY = "minority"


class IssuerLevel(StrEnum):
    UNIVERSITY = "university"
    COLLEGE = "college"
    DEPARTMENT = "department"


class PolicyRelation(StrEnum):
    STANDALONE = "standalone"
    IMPLEMENTS = "implements"
    SUPPLEMENTS = "supplements"


class AuthorityLevel(StrEnum):
    BINDING = "binding"
    OPERATIONAL = "operational"
    INFORMATIONAL = "informational"
    UNKNOWN = "unknown"


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
    MIXED = "mixed"
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
    PARTIAL = "partial"
    ABSTAIN = "abstain"


class GraphDecision(StrEnum):
    """W6 图内决策；不替换 W3 的 VerificationDecision。"""

    PASS = "pass"
    REVISE = "revise"
    CLARIFY = "clarify"
    ABSTAIN = "abstain"


class IssueScope(StrEnum):
    CLAIM = "claim"
    CITATION = "citation"
    EVIDENCE = "evidence"


class IssueSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


class FactKind(StrEnum):
    NUMBER = "number"
    PERCENT = "percent"
    DATE = "date"
    DURATION = "duration"
    AMOUNT = "amount"


class OutcomeCode(StrEnum):
    ANSWERED = "ANSWERED"
    PARTIAL_ANSWER = "PARTIAL_ANSWER"
    UNVERIFIABLE = "UNVERIFIABLE"
    ONLY_REPEALED = "ONLY_REPEALED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    NO_EVIDENCE = "NO_EVIDENCE"
    HISTORICAL_NOT_SUPPORTED = "HISTORICAL_NOT_SUPPORTED"
    PRIVACY_REQUEST = "PRIVACY_REQUEST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    INVALID_REQUEST = "INVALID_REQUEST"
    GENERATION_FAILED = "GENERATION_FAILED"
    NO_VALID_CLAIM = "NO_VALID_CLAIM"
    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"


class DocType(StrEnum):
    REGULATION = "regulation"
    NOTICE = "notice"
    GUIDE = "guide"
    FAQ = "faq"
    REPORT = "report"
    OTHER = "other"


class BlockType(StrEnum):
    ARTICLE = "article"
    SECTION = "section"
    PREFACE = "preface"
    APPENDIX = "appendix"
    TABLE = "table"
    OTHER = "other"
