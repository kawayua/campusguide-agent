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
    REPORT = "report"
    OTHER = "other"


class BlockType(StrEnum):
    ARTICLE = "article"
    SECTION = "section"
    PREFACE = "preface"
    APPENDIX = "appendix"
    TABLE = "table"
    OTHER = "other"
