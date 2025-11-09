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


class GameSessionCreate(BaseModel):
    """Request model for creating a game session"""

    game_id: str = Field(..., description="Unique game session ID")
    email: EmailStr = Field(..., description="User email")


class GameSessionEnd(BaseModel):
    """Request model for ending a game session"""

    game_id: str
    outcome: str = Field(..., description="'win' or 'lose'")
    selected_researcher: str
    selected_planet: str


class GameSessionResponse(BaseModel):
    """Response model for game session"""

    game_id: str
    email: str
    start_time: datetime
    end_time: Optional[datetime]
    outcome: Optional[str]
    selected_researcher: Optional[str]
    selected_planet: Optional[str]


class GameChatMessage(BaseModel):
    """Single chat message in a game"""

    role: str = Field(..., description="'user' or 'assistant'")
    content: str
    message_order: int


class GameChatLogCreate(BaseModel):
    """Request model for saving game chat logs"""

    game_id: str
    email: EmailStr
    researcher_name: str
    planet_name: str
    messages: List[GameChatMessage]


class GameChatLogResponse(BaseModel):
    """Response model for game chat log"""

    game_id: str
    researcher_name: str
    planet_name: str
    messages: List[GameChatMessage]
    timestamp: datetime
    outcome: Optional[str]


class ChatLogWithMessages(BaseModel):
    """Chat log for a single researcher with messages"""

    researcher_name: str
    planet_name: str
    messages: List[GameChatMessage]


class GameSessionWithChatsResponse(BaseModel):
    """Response model for game session with all chat logs"""

    game_id: str
    email: str
    start_time: datetime
    end_time: Optional[datetime]
    outcome: Optional[str]
    selected_researcher: Optional[str]
    selected_planet: Optional[str]
    chat_logs: List[ChatLogWithMessages]


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


async def create_game_session(
    conn: asyncpg.Connection, game_id: str, email: str
) -> dict:
    """Create a new game session"""
    session = await conn.fetchrow(
        """
        INSERT INTO game_sessions (game_id, email)
        VALUES ($1, $2)
        RETURNING game_id, email, start_time, end_time, outcome, selected_researcher, selected_planet
        """,
        game_id,
        email,
    )
    return dict(session)


async def end_game_session(
    conn: asyncpg.Connection,
    game_id: str,
    outcome: str,
    selected_researcher: str,
    selected_planet: str,
) -> dict:
    """End a game session with outcome"""
    session = await conn.fetchrow(
        """
        UPDATE game_sessions
        SET end_time = CURRENT_TIMESTAMP,
            outcome = $2,
            selected_researcher = $3,
            selected_planet = $4
        WHERE game_id = $1
        RETURNING game_id, email, start_time, end_time, outcome, selected_researcher, selected_planet
        """,
        game_id,
        outcome,
        selected_researcher,
        selected_planet,
    )

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    return dict(session)


async def save_game_chat_log(
    conn: asyncpg.Connection,
    game_id: str,
    email: str,
    researcher_name: str,
    planet_name: str,
    messages: List[GameChatMessage],
) -> None:
    """Save all chat messages for a researcher in a game session"""
    # Delete existing messages for this game/researcher combo (in case of re-save)
    await conn.execute(
        """
        DELETE FROM game_chat_logs
        WHERE game_id = $1 AND researcher_name = $2
        """,
        game_id,
        researcher_name,
    )

    # Insert all messages
    if messages:
        values = [
            (
                game_id,
                email,
                researcher_name,
                planet_name,
                msg.role,
                msg.content,
                msg.message_order,
            )
            for msg in messages
        ]

        await conn.executemany(
            """
            INSERT INTO game_chat_logs (game_id, email, researcher_name, planet_name, role, content, message_order)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            values,
        )


async def get_researcher_chat_logs(
    conn: asyncpg.Connection,
    email: str,
    researcher_name: str,
) -> List[dict]:
    """Get all chat logs for a specific researcher across all games for a user"""
    # Get all game sessions for this researcher
    rows = await conn.fetch(
        """
        SELECT DISTINCT
            gcl.game_id,
            gcl.researcher_name,
            gcl.planet_name,
            gs.start_time,
            gs.outcome
        FROM game_chat_logs gcl
        JOIN game_sessions gs ON gcl.game_id = gs.game_id
        WHERE gcl.email = $1 AND gcl.researcher_name = $2
        ORDER BY gs.start_time DESC
        """,
        email,
        researcher_name,
    )

    result = []
    for row in rows:
        # Get all messages for this game/researcher
        messages = await conn.fetch(
            """
            SELECT role, content, message_order
            FROM game_chat_logs
            WHERE game_id = $1 AND researcher_name = $2
            ORDER BY message_order ASC
            """,
            row["game_id"],
            researcher_name,
        )

        result.append(
            {
                "game_id": row["game_id"],
                "researcher_name": row["researcher_name"],
                "planet_name": row["planet_name"],
                "timestamp": row["start_time"],
                "outcome": row["outcome"],
                "messages": [
                    {
                        "role": msg["role"],
                        "content": msg["content"],
                        "message_order": msg["message_order"],
                    }
                    for msg in messages
                ],
            }
        )

    return result


async def get_all_game_sessions(
    conn: asyncpg.Connection,
    email: str,
) -> List[dict]:
    """Get all game sessions for a user"""
    rows = await conn.fetch(
        """
        SELECT game_id, email, start_time, end_time, outcome, selected_researcher, selected_planet
        FROM game_sessions
        WHERE email = $1
        ORDER BY start_time DESC
        """,
        email,
    )

    return [dict(row) for row in rows]


async def get_game_session_with_chats(
    conn: asyncpg.Connection,
    game_id: str,
) -> dict:
    """Get complete game session with all chat logs"""
    # Get the game session
    session = await conn.fetchrow(
        """
        SELECT game_id, email, start_time, end_time, outcome, selected_researcher, selected_planet
        FROM game_sessions
        WHERE game_id = $1
        """,
        game_id,
    )

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    # Get all unique researchers for this game
    researchers = await conn.fetch(
        """
        SELECT DISTINCT researcher_name, planet_name
        FROM game_chat_logs
        WHERE game_id = $1
        ORDER BY researcher_name
        """,
        game_id,
    )

    # Get chat logs for each researcher
    chat_logs = []
    for researcher_row in researchers:
        researcher_name = researcher_row["researcher_name"]
        planet_name = researcher_row["planet_name"]

        # Get all messages for this researcher
        messages = await conn.fetch(
            """
            SELECT role, content, message_order
            FROM game_chat_logs
            WHERE game_id = $1 AND researcher_name = $2
            ORDER BY message_order ASC
            """,
            game_id,
            researcher_name,
        )

        chat_logs.append(
            {
                "researcher_name": researcher_name,
                "planet_name": planet_name,
                "messages": [
                    {
                        "role": msg["role"],
                        "content": msg["content"],
                        "message_order": msg["message_order"],
                    }
                    for msg in messages
                ],
            }
        )

    return {
        "game_id": session["game_id"],
        "email": session["email"],
        "start_time": session["start_time"],
        "end_time": session["end_time"],
        "outcome": session["outcome"],
        "selected_researcher": session["selected_researcher"],
        "selected_planet": session["selected_planet"],
        "chat_logs": chat_logs,
    }


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
async def create_or_get_user_endpoint(user_data: UserCreate, conn=Depends(get_db)):
    """
    Create a new user or get existing user by email
    This should be called by the frontend after authentication
    """
    try:
        user = await get_or_create_user(conn, user_data.email)
        return UserResponse(**user)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create/get user: {str(e)}"
        )


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
        raise HTTPException(
            status_code=500, detail=f"Failed to add chat message: {str(e)}"
        )


@app.get("/api/chat/{email}", response_model=List[ChatMessageResponse])
async def get_chat(email: str, limit: int = 100, conn=Depends(get_db)):
    """Get chat history for a user"""
    try:
        messages = await get_chat_history(conn, email, limit)
        return [ChatMessageResponse(**msg) for msg in messages]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get chat history: {str(e)}"
        )


@app.post("/api/game-sessions", response_model=GameSessionResponse)
async def create_session(session_data: GameSessionCreate, conn=Depends(get_db)):
    """Create a new game session"""
    try:
        session = await create_game_session(
            conn, session_data.game_id, session_data.email
        )
        return GameSessionResponse(**session)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create game session: {str(e)}"
        )


@app.get("/api/game-sessions/{email}", response_model=List[GameSessionResponse])
async def get_user_game_sessions(email: str, conn=Depends(get_db)):
    """Get all game sessions for a user"""
    try:
        sessions = await get_all_game_sessions(conn, email)
        return [GameSessionResponse(**session) for session in sessions]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get game sessions: {str(e)}"
        )


@app.get(
    "/api/game-sessions/{game_id}/chats", response_model=GameSessionWithChatsResponse
)
async def get_session_with_chats(game_id: str, conn=Depends(get_db)):
    """Get complete game session with all chat logs"""
    try:
        session_data = await get_game_session_with_chats(conn, game_id)
        return GameSessionWithChatsResponse(**session_data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get game session with chats: {str(e)}"
        )


@app.post("/api/game-sessions/{game_id}/end", response_model=GameSessionResponse)
async def end_session(game_id: str, end_data: GameSessionEnd, conn=Depends(get_db)):
    """End a game session with outcome"""
    try:
        if game_id != end_data.game_id:
            raise HTTPException(status_code=400, detail="Game ID mismatch")

        session = await end_game_session(
            conn,
            game_id,
            end_data.outcome,
            end_data.selected_researcher,
            end_data.selected_planet,
        )
        return GameSessionResponse(**session)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to end game session: {str(e)}"
        )


@app.post("/api/game-chat-logs")
async def save_chat_log(log_data: GameChatLogCreate, conn=Depends(get_db)):
    """Save chat log for a researcher in a game session"""
    try:
        await save_game_chat_log(
            conn,
            log_data.game_id,
            log_data.email,
            log_data.researcher_name,
            log_data.planet_name,
            log_data.messages,
        )
        return {"status": "success", "message": "Chat log saved"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to save chat log: {str(e)}"
        )


@app.get(
    "/api/game-chat-logs/{email}/{researcher_name}",
    response_model=List[GameChatLogResponse],
)
async def get_researcher_logs(email: str, researcher_name: str, conn=Depends(get_db)):
    """Get all chat logs for a specific researcher across all games"""
    try:
        logs = await get_researcher_chat_logs(conn, email, researcher_name)
        return [GameChatLogResponse(**log) for log in logs]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get researcher chat logs: {str(e)}"
        )


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
