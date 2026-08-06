from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlmodel import SQLModel, Field, Relationship

if TYPE_CHECKING:
    from models.user import User


# =====================================================
# DOCUMENT MODEL
# =====================================================

class Document(SQLModel, table=True):
    id: Optional[int] = Field(
        default=None,
        primary_key=True,
    )

    filename: str

    original_filename: str

    # Versioning (Exercise 2)
    version: int = Field(default=1)

    file_size: int

    file_type: str

    status: str = Field(default="uploaded")

    # Delivery location
    city: str = Field(index=True)

    country: str = Field(default="Kenya")

    # Weather enrichment
    weather_data: Optional[str] = None

    weather_fetched_at: Optional[datetime] = None

    # Metadata
    description: Optional[str] = None

    uploader_id: int = Field(
        foreign_key="user.id"
    )

    uploader: Optional["User"] = Relationship(
        back_populates="documents"
    )

    uploaded_at: datetime = Field(
        default_factory=datetime.utcnow
    )

    updated_at: datetime = Field(
        default_factory=datetime.utcnow
    )

    file_path: str


# =====================================================
# CREATE MODEL
# =====================================================

class DocumentCreate(SQLModel):
    city: str = Field(
        min_length=2,
        max_length=100,
    )

    country: str = Field(
        default="Kenya",
        min_length=2,
        max_length=100,
    )

    description: Optional[str] = None


# =====================================================
# UPDATE MODEL
# =====================================================

class DocumentUpdate(SQLModel):
    city: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=100,
    )

    country: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=100,
    )

    description: Optional[str] = None