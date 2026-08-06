from datetime import datetime
import json
import os

import aiofiles

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
    UploadFile,
    File,
    Form,
    status,
)

from fastapi.security import OAuth2PasswordRequestForm

from sqlmodel import Session, select

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.extension import _rate_limit_exceeded_handler

from database.session import (
    create_tables,
    get_session,
)

from models.user import (
    User,
    UserCreate,
    UserResponse,
)

from models.document import (
    Document,
    DocumentCreate,
    DocumentUpdate,
)

from auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    get_current_admin,
    get_current_manager,
)

from services.weather import get_weather

app = FastAPI(
    title="SendIt API",
    version="1.0.0",
)

create_tables()
from typing import Optional
from services.webhook import (
    register_webhook,
    send_webhook,
)
# =====================================================
# FILE UPLOAD CONFIGURATION
# =====================================================

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_FILE_SIZE = int(
    os.getenv(
        "MAX_UPLOAD_SIZE",
        5 * 1024 * 1024,
    )
)

ALLOWED_EXTENSIONS = [
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".docx",
]

# =====================================================
# RATE LIMITING
# =====================================================

limiter = Limiter(
    key_func=get_remote_address
)

app.state.limiter = limiter

app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)

app.add_middleware(
    SlowAPIMiddleware
)

# =====================================================
# AUTHENTICATION
# =====================================================

@app.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    user_data: UserCreate,
    session: Session = Depends(get_session),
):
    """
    Register a new user.
    """

    existing_user = session.exec(
        select(User).where(
            User.username == user_data.username
        )
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Username already exists",
        )

    existing_email = session.exec(
        select(User).where(
            User.email == user_data.email
        )
    ).first()

    if existing_email:
        raise HTTPException(
            status_code=400,
            detail="Email already exists",
        )

    user = User(
        username=user_data.username,
        email=user_data.email,
        full_name=user_data.full_name,
        role=user_data.role,
        hashed_password=hash_password(
            user_data.password
        ),
    )

    session.add(user)
    session.commit()
    session.refresh(user)

    return user


@app.post("/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
):
    """
    Login and return JWT token.
    """

    user = session.exec(
        select(User).where(
            User.username == form_data.username
        )
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
        )

    if not verify_password(
        form_data.password,
        user.hashed_password,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
        )

    user.last_login = datetime.utcnow()

    session.add(user)
    session.commit()

    token = create_access_token(
        {
            "sub": user.username,
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer",
    }


# =====================================================
# FILE VALIDATION
# =====================================================

def validate_file(
    file: UploadFile,
):
    """
    Validate uploaded file.
    """

    extension = os.path.splitext(
        file.filename
    )[1].lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Allowed file types: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    return extension
# =====================================================
# DOCUMENT UPLOAD
# =====================================================

@app.post("/documents/upload")
@limiter.limit("10/hour")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    city: str = Form(...),
    description: str | None = Form(None),
    country: str = Form("Kenya"),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Upload a document.
    """

    # Validate file
    validate_file(file)

    contents = await file.read()
    file_size = len(contents)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum file size is {MAX_FILE_SIZE // (1024 * 1024)} MB",
        )

    # Create safe filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    safe_filename = (
        f"{timestamp}_{current_user.id}_"
        f"{file.filename.replace(' ', '_')}"
    )

    file_path = os.path.join(
        UPLOAD_DIR,
        safe_filename,
    )

    # Save file
    async with aiofiles.open(file_path, "wb") as out_file:
        await out_file.write(contents)

    # Create document
    document = Document(
        filename=safe_filename,
        original_filename=file.filename,
        file_size=file_size,
        file_type=file.content_type or "application/octet-stream",
        city=city,
        country=country,
        description=description,
        uploader_id=current_user.id,
        file_path=file_path,
        status="processing",
    )

    session.add(document)
    session.commit()
    session.refresh(document)

    # Weather enrichment
    try:
        weather = await get_weather(city, country)

        if weather:
            document.weather_data = json.dumps(weather)
            document.weather_fetched_at = datetime.utcnow()
            document.status = "enriched"
        else:
            document.status = "uploaded"

    except Exception as e:
        print(f"Weather API Error: {e}")
        document.status = "uploaded"

    session.add(document)
    session.commit()
    session.refresh(document)

    # Send webhook notification
    await send_webhook(
        "document.uploaded",
        {
            "document_id": document.id,
            "filename": document.original_filename,
            "status": document.status,
        },
    )

    return {
        "message": "Document uploaded successfully",
        "document_id": document.id,
        "filename": document.original_filename,
        "status": document.status,
    }
# =====================================================
# LIST DOCUMENTS
# =====================================================

@app.get("/documents")
@limiter.limit("30/minute")
def list_documents(
    request: Request,
    status: str | None = None,
    city: str | None = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    List documents.
    """

    query = select(Document)

    if current_user.role not in [
        "admin",
        "manager",
    ]:

        query = query.where(
            Document.uploader_id == current_user.id
        )

    if status:

        query = query.where(
            Document.status == status
        )

    if city:

        query = query.where(
            Document.city == city
        )

    return session.exec(query).all()


# =====================================================
# GET DOCUMENT
# =====================================================

@app.get("/documents/{document_id}")
@limiter.limit("30/minute")
def get_document(
    request: Request,
    document_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Retrieve one document.
    """

    document = session.get(
        Document,
        document_id,
    )

    if not document:

        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if (
        current_user.role not in [
            "admin",
            "manager",
        ]
        and document.uploader_id != current_user.id
    ):

        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    return document


# =====================================================
# DELETE DOCUMENT
# =====================================================

@app.delete("/documents/{document_id}")
def delete_document(
    document_id: int,
    current_user: User = Depends(get_current_manager),
    session: Session = Depends(get_session),
):
    """
    Delete document.
    """

    document = session.get(
        Document,
        document_id,
    )

    if not document:

        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if os.path.exists(
        document.file_path
    ):

        os.remove(
            document.file_path
        )

    session.delete(document)
    session.commit()

    return {
        "message": "Document deleted successfully"
    }
# =====================================================
# DOCUMENT ENRICHMENT
# =====================================================

@app.post("/documents/{document_id}/enrich")
@limiter.limit("5/minute")
async def enrich_document(
    request: Request,
    document_id: int,
    current_user: User = Depends(get_current_manager),
    session: Session = Depends(get_session),
):
    """
    Manually enrich a document with weather data.
    """

    document = session.get(Document, document_id)

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if document.status == "enriched":
        return {
            "message": "Document already enriched"
        }

    weather = await get_weather(
        document.city,
        document.country,
    )

    if not weather:
        document.status = "failed"
        session.add(document)
        session.commit()

        raise HTTPException(
            status_code=500,
            detail="Weather enrichment failed",
        )

    # Save weather data
    document.weather_data = json.dumps(weather)
    document.weather_fetched_at = datetime.utcnow()
    document.status = "enriched"

    session.add(document)
    session.commit()
    session.refresh(document)

    # Send webhook notification
    await send_webhook(
        "document.enriched",
        {
            "document_id": document.id,
            "filename": document.original_filename,
            "status": document.status,
            "weather": weather,
        },
    )

    return {
        "message": "Document enriched successfully",
        "document_id": document.id,
        "filename": document.original_filename,
        "status": document.status,
        "weather": weather,
    }
# =====================================================
# DOCUMENT WEATHER
# =====================================================

@app.get("/documents/{document_id}/weather")
@limiter.limit("10/minute")
def get_document_weather(
    request: Request,
    document_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Retrieve stored weather information.
    """

    document = session.get(
        Document,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if (
        current_user.role not in [
            "admin",
            "manager",
        ]
        and document.uploader_id != current_user.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    if not document.weather_data:
        raise HTTPException(
            status_code=404,
            detail="No weather data available",
        )

    return {
        "document_id": document.id,
        "city": document.city,
        "country": document.country,
        "weather": json.loads(document.weather_data),
    }


# =====================================================
# SEARCH DOCUMENTS
# =====================================================

@app.get("/documents/search")
@limiter.limit("20/minute")
def search_documents(
    request: Request,
    q: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Search documents with multiple filters.
    """

    query = select(Document)

    # Staff only see their own documents
    if current_user.role not in [
        "admin",
        "manager",
    ]:
        query = query.where(
            Document.uploader_id == current_user.id
        )

    if q:
        query = query.where(
            Document.original_filename.contains(q)
            | Document.description.contains(q)
        )

    if city:
        query = query.where(
            Document.city == city
        )

    if status:
        query = query.where(
            Document.status == status
        )

    if date_from:
        query = query.where(
            Document.uploaded_at >= date_from
        )

    if date_to:
        query = query.where(
            Document.uploaded_at <= date_to
        )

    return session.exec(query).all()


# =====================================================
# DOCUMENT VERSIONING
# =====================================================

def get_next_document_version(
    session: Session,
    filename: str,
) -> int:
    """
    Determine the next version number for a document.
    """

    existing = session.exec(
        select(Document).where(
            Document.original_filename == filename
        )
    ).all()

    if not existing:
        return 1

    return max(doc.version for doc in existing) + 1
# =====================================================
# DOCUMENT ENRICHMENT
# =====================================================

@app.post("/documents/{document_id}/enrich")
@limiter.limit("5/minute")
async def enrich_document(
    request: Request,
    document_id: int,
    current_user: User = Depends(get_current_manager),
    session: Session = Depends(get_session),
):
    """
    Manually enrich a document with weather data.
    """

    document = session.get(
        Document,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if document.status == "enriched":
        return {
            "message": "Document already enriched"
        }

    weather = await get_weather(
        document.city,
        document.country,
    )

    if weather:

        document.weather_data = json.dumps(weather)

        document.weather_fetched_at = datetime.utcnow()

        document.status = "enriched"

        session.add(document)
        session.commit()
        session.refresh(document)

        return {
            "message": "Document enriched successfully",
            "weather": weather,
        }

    document.status = "failed"

    session.add(document)
    session.commit()

    raise HTTPException(
        status_code=500,
        detail="Weather enrichment failed",
    )


# =====================================================
# DOCUMENT WEATHER
# =====================================================

@app.get("/documents/{document_id}/weather")
@limiter.limit("10/minute")
def get_document_weather(
    request: Request,
    document_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Retrieve stored weather information.
    """

    document = session.get(
        Document,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    if (
        current_user.role not in [
            "admin",
            "manager",
        ]
        and document.uploader_id != current_user.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    if not document.weather_data:
        raise HTTPException(
            status_code=404,
            detail="No weather data available",
        )

    return {
        "document_id": document.id,
        "city": document.city,
        "country": document.country,
        "weather": json.loads(document.weather_data),
    }


# =====================================================
# SEARCH DOCUMENTS
# =====================================================

@app.get("/documents/search")
@limiter.limit("20/minute")
def search_documents(
    request: Request,
    q: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Search documents with multiple filters.
    """

    query = select(Document)

    # Staff only see their own documents
    if current_user.role not in [
        "admin",
        "manager",
    ]:
        query = query.where(
            Document.uploader_id == current_user.id
        )

    if q:
        query = query.where(
            Document.original_filename.contains(q)
            | Document.description.contains(q)
        )

    if city:
        query = query.where(
            Document.city == city
        )

    if status:
        query = query.where(
            Document.status == status
        )

    if date_from:
        query = query.where(
            Document.uploaded_at >= date_from
        )

    if date_to:
        query = query.where(
            Document.uploaded_at <= date_to
        )

    return session.exec(query).all()


# =====================================================
# DOCUMENT VERSIONING
# =====================================================

def get_next_document_version(
    session: Session,
    filename: str,
) -> int:
    """
    Determine the next version number for a document.
    """

    existing = session.exec(
        select(Document).where(
            Document.original_filename == filename
        )
    ).all()

    if not existing:
        return 1

    return max(doc.version for doc in existing) + 1
@app.post("/webhooks/register")
def register_webhook_endpoint(
    webhook_url: str,
    event_type: str,
    current_user: User = Depends(get_current_admin),
):
    return register_webhook(
        event_type,
        webhook_url,
    )
