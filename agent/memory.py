# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, delete
from dotenv import load_dotenv

logger = logging.getLogger("agentkit")

load_dotenv()

# Configuración de base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Si es PostgreSQL en producción, ajustar el esquema de URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ControlHumano(Base):
    """Conversaciones donde un operador humano tomó el control."""
    __tablename__ = "control_humano"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    operador: Mapped[str] = mapped_column(String(50))   # "abdiel" o "grace"
    desde: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificacionEquipo(Base):
    """
    Notificaciones enviadas al equipo. Guarda el ID del mensaje de WhatsApp
    enviado a Grace para poder vincular su respuesta (reply/cita) con el
    cliente correcto y reenviarle la contestación.
    """
    __tablename__ = "notificaciones_equipo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mensaje_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    telefono_cliente: Mapped[str] = mapped_column(String(50), index=True)
    resumen: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")  # pendiente | respondida
    creado: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

        # Invertir para orden cronológico (los más recientes están primero)
        mensajes.reverse()

        return [
            {"role": msg.role, "content": msg.content}
            for msg in mensajes
        ]


async def pausar_conversacion(telefono: str, operador: str):
    """Pausa el bot para esta conversación y asigna un operador humano."""
    async with async_session() as session:
        # Upsert: si ya existe, actualiza el operador
        existente = await session.get(ControlHumano, telefono)
        if existente:
            existente.operador = operador
            existente.desde = datetime.utcnow()
        else:
            session.add(ControlHumano(telefono=telefono, operador=operador))
        await session.commit()
    logger.info(f"Conversacion {telefono} pausada — operador: {operador}")


async def reanudar_conversacion(telefono: str):
    """Devuelve el control al bot para esta conversación."""
    async with async_session() as session:
        await session.execute(
            delete(ControlHumano).where(ControlHumano.telefono == telefono)
        )
        await session.commit()
    logger.info(f"Conversacion {telefono} reanudada — bot retoma el control")


async def esta_pausada(telefono: str) -> bool:
    """Retorna True si la conversación está en manos de un operador humano."""
    async with async_session() as session:
        resultado = await session.get(ControlHumano, telefono)
        return resultado is not None


async def guardar_notificacion(mensaje_id: str, telefono_cliente: str, resumen: str):
    """Registra una notificación enviada al equipo, vinculada al ID del mensaje de WhatsApp."""
    async with async_session() as session:
        session.add(NotificacionEquipo(
            mensaje_id=mensaje_id,
            telefono_cliente=telefono_cliente,
            resumen=resumen,
        ))
        await session.commit()
    logger.info(f"Notificacion registrada: mensaje {mensaje_id} → cliente {telefono_cliente}")


async def buscar_notificacion(mensaje_id: str) -> dict | None:
    """
    Busca la notificación asociada a un mensaje enviado al equipo.
    Se usa cuando Grace responde citando la notificación.
    """
    async with async_session() as session:
        query = select(NotificacionEquipo).where(NotificacionEquipo.mensaje_id == mensaje_id)
        result = await session.execute(query)
        notificacion = result.scalar_one_or_none()
        if notificacion is None:
            return None
        return {
            "telefono_cliente": notificacion.telefono_cliente,
            "resumen": notificacion.resumen,
            "estado": notificacion.estado,
        }


async def marcar_notificacion_respondida(mensaje_id: str):
    """Marca una notificación como respondida por el equipo."""
    async with async_session() as session:
        query = select(NotificacionEquipo).where(NotificacionEquipo.mensaje_id == mensaje_id)
        result = await session.execute(query)
        notificacion = result.scalar_one_or_none()
        if notificacion:
            notificacion.estado = "respondida"
            await session.commit()


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()
