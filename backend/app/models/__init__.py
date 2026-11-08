"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from app.models.article import Article
from app.models.article_embedding import ArticleEmbedding
from app.models.commodity_group import CommodityGroup
from app.models.organization import (
    Organization,
    OrganizationInvite,
    OrganizationMember,
)
from app.models.procurement_request import (
    OrderLine,
    ProcurementRequest,
    RequestActivity,
    RequestDocument,
)
from app.models.request_draft import RequestDraft
from app.models.supplier import Supplier
from app.models.user import Profile, User

__all__ = [
    "User",
    "Profile",
    "CommodityGroup",
    "Organization",
    "OrganizationMember",
    "OrganizationInvite",
    "ProcurementRequest",
    "OrderLine",
    "RequestActivity",
    "RequestDocument",
    "RequestDraft",
    "Supplier",
    "Article",
    "ArticleEmbedding",
]
