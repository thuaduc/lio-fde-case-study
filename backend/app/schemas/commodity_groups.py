from pydantic import BaseModel, Field


class CommodityGroupCreate(BaseModel):
    category: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None


class CommodityGroupUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
