from typing import Annotated

from pydantic import BaseModel, StringConstraints

# Category and name must be non-empty after trimming (422 otherwise).
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CommodityGroupCreate(BaseModel):
    category: NonBlank
    name: NonBlank
    description: str | None = None


class CommodityGroupUpdate(BaseModel):
    category: NonBlank | None = None
    name: NonBlank | None = None
    description: str | None = None
