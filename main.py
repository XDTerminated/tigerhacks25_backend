"""
Social Deduction Space Game Backend API
Handles user management, player stats, and chat history
"""

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime
import os
from contextlib import asynccontextmanager
import asyncpg
from asyncpg.pool import Pool
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

# ============================================================================
# Database Connection Pool
# ============================================================================

db_pool: Optional[Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage database connection pool lifecycle"""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
    print("✅ Database connection pool created")
    yield
    await db_pool.close()
    print("❌ Database connection pool closed")


# ============================================================================
# FastAPI App Setup
# ============================================================================

app = FastAPI(
    title="Social Deduction Space Game API",
    description="Backend API for social deduction space survival game",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this to your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Dependency: Database Connection
# ============================================================================


async def get_db():
    """Get database connection from pool"""
    if db_pool is None:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    async with db_pool.acquire() as connection:
        yield connection


# ============================================================================
# Pydantic Models
# ============================================================================


class UserCreate(BaseModel):
    """Request model for creating a user"""
    email: EmailStr = Field(..., description="User email address")


class UserResponse(BaseModel):
    """Response model for user data"""
    email: str
    last_login: datetime
    created_at: datetime


class PlayerStatsResponse(BaseModel):
    """Response model for player statistics"""
    email: str
    correct_guesses: int
    correct_ejections: int
    incorrect_guesses: int
    updated_at: datetime


class ChatMessageCreate(BaseModel):
    """Request model for creating a chat message"""
    email: EmailStr = Field(..., description="User email")
    speaker: str = Field(..., description="Speaker name (player or NPC name)")
    message: str = Field(..., description="Message content")


class ChatMessageResponse(BaseModel):
    """Response model for chat message"""
    id: int
    email: str
    speaker: str
    message: str
    timestamp: datetime


class StatsUpdate(BaseModel):
    """Request model for updating player stats"""
    email: EmailStr
    correct_guesses: Optional[int] = None
    correct_ejections: Optional[int] = None
    incorrect_guesses: Optional[int] = None


# ============================================================================
# Database Functions
# ============================================================================


async def get_or_create_user(conn: asyncpg.Connection, email: str) -> dict:
    """Get existing user or create new one"""
    # Try to get existing user
    user = await conn.fetchrow(
        """
        SELECT email, last_login, created_at
        FROM users
        WHERE email = $1
        """,
        email,
    )

    if user:
        # Update last login
        user = await conn.fetchrow(
            """
            UPDATE users
            SET last_login = CURRENT_TIMESTAMP
            WHERE email = $1
            RETURNING email, last_login, created_at
            """,
            email,
        )
        return dict(user)

    # Create new user and initialize stats
    async with conn.transaction():
        user = await conn.fetchrow(
            """
            INSERT INTO users (email)
            VALUES ($1)
            RETURNING email, last_login, created_at
            """,
            email,
        )

        # Initialize player stats
        await conn.execute(
            """
            INSERT INTO player_stats (email)
            VALUES ($1)
            """,
            email,
        )

    return dict(user)


async def get_player_stats(conn: asyncpg.Connection, email: str) -> dict:
    """Get player statistics"""
    stats = await conn.fetchrow(
        """
        SELECT email, correct_guesses, correct_ejections, incorrect_guesses, updated_at
        FROM player_stats
        WHERE email = $1
        """,
        email,
    )

    if not stats:
        raise HTTPException(status_code=404, detail="Player stats not found")

    return dict(stats)


async def update_player_stats(
    conn: asyncpg.Connection,
    email: str,
    correct_guesses: Optional[int],
    correct_ejections: Optional[int],
    incorrect_guesses: Optional[int],
) -> dict:
    """Update player statistics (incremental)"""

    # Build dynamic update query
    updates = []
    params = [email]
    param_count = 2

    if correct_guesses is not None:
        updates.append(f"correct_guesses = correct_guesses + ${param_count}")
        params.append(correct_guesses)
        param_count += 1

    if correct_ejections is not None:
        updates.append(f"correct_ejections = correct_ejections + ${param_count}")
        params.append(correct_ejections)
        param_count += 1

    if incorrect_guesses is not None:
        updates.append(f"incorrect_guesses = incorrect_guesses + ${param_count}")
        params.append(incorrect_guesses)
        param_count += 1

    if not updates:
        # No updates, just return current stats
        return await get_player_stats(conn, email)

    updates.append("updated_at = CURRENT_TIMESTAMP")

    query = f"""
        UPDATE player_stats
        SET {', '.join(updates)}
        WHERE email = $1
        RETURNING email, correct_guesses, correct_ejections, incorrect_guesses, updated_at
    """

    stats = await conn.fetchrow(query, *params)

    if not stats:
        raise HTTPException(status_code=404, detail="Player stats not found")

    return dict(stats)


async def add_chat_message(
    conn: asyncpg.Connection, email: str, speaker: str, message: str
) -> dict:
    """Add a chat message to history"""
    chat = await conn.fetchrow(
        """
        INSERT INTO chat_history (email, speaker, message)
        VALUES ($1, $2, $3)
        RETURNING id, email, speaker, message, timestamp
        """,
        email,
        speaker,
        message,
    )

    return dict(chat)


async def get_chat_history(
    conn: asyncpg.Connection, email: str, limit: int = 100
) -> List[dict]:
    """Get chat history for a user"""
    rows = await conn.fetch(
        """
        SELECT id, email, speaker, message, timestamp
        FROM chat_history
        WHERE email = $1
        ORDER BY timestamp DESC
        LIMIT $2
        """,
        email,
        limit,
    )

    return [dict(row) for row in rows]


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Social Deduction Space Game API",
        "version": "1.0.0",
    }


@app.post("/api/users", response_model=UserResponse)
async def create_or_get_user(user_data: UserCreate, conn=Depends(get_db)):
    """
    Create a new user or get existing user by email
    This should be called by the frontend after authentication
    """
    try:
        user = await get_or_create_user(conn, user_data.email)
        return UserResponse(**user)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create/get user: {str(e)}")


@app.get("/api/users/{email}", response_model=UserResponse)
async def get_user(email: str, conn=Depends(get_db)):
    """Get user by email"""
    user = await conn.fetchrow(
        """
        SELECT email, last_login, created_at
        FROM users
        WHERE email = $1
        """,
        email,
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(**dict(user))


@app.get("/api/stats/{email}", response_model=PlayerStatsResponse)
async def get_stats(email: str, conn=Depends(get_db)):
    """Get player statistics"""
    try:
        stats = await get_player_stats(conn, email)
        return PlayerStatsResponse(**stats)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")


@app.post("/api/stats", response_model=PlayerStatsResponse)
async def update_stats(stats_data: StatsUpdate, conn=Depends(get_db)):
    """
    Update player statistics (incremental)
    Only increments the provided fields
    """
    try:
        stats = await update_player_stats(
            conn,
            stats_data.email,
            stats_data.correct_guesses,
            stats_data.correct_ejections,
            stats_data.incorrect_guesses,
        )
        return PlayerStatsResponse(**stats)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update stats: {str(e)}")


@app.post("/api/chat", response_model=ChatMessageResponse)
async def add_message(chat_data: ChatMessageCreate, conn=Depends(get_db)):
    """Add a chat message to history"""
    try:
        chat = await add_chat_message(
            conn, chat_data.email, chat_data.speaker, chat_data.message
        )
        return ChatMessageResponse(**chat)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add chat message: {str(e)}")


@app.get("/api/chat/{email}", response_model=List[ChatMessageResponse])
async def get_chat(email: str, limit: int = 100, conn=Depends(get_db)):
    """Get chat history for a user"""
    try:
        messages = await get_chat_history(conn, email, limit)
        return [ChatMessageResponse(**msg) for msg in messages]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get chat history: {str(e)}")


# ============================================================================
# Run Server
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # Enable auto-reload during development
    )
