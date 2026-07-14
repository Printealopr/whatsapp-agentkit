# agent/providers/whapi.py — Adaptador para Whapi.cloud
# Generado por AgentKit

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


def _normalizar_telefono(chat_id: str) -> str:
    """
    Normaliza el chat_id de Whapi eliminando sufijos como @c.us o @s.whatsapp.net.
    Whapi puede enviarlo con o sin sufijo según si el mensaje es entrante o saliente.
    Sin normalizar, los números no coinciden en la base de datos y la pausa no funciona.
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.cloud (REST API simple)."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.url_envio = "https://gate.whapi.cloud/messages/text"
        self.url_base = "https://gate.whapi.cloud"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Whapi.cloud. Detecta texto, imágenes y documentos."""
        body = await request.json()
        mensajes = []
        for msg in body.get("messages", []):
            tipo = msg.get("type", "text")

            if tipo == "text":
                texto = msg.get("text", {}).get("body", "")
            elif tipo == "image":
                # El cliente envió una imagen — probablemente es su arte
                caption = msg.get("image", {}).get("caption", "")
                texto = f"[ARTE RECIBIDO - imagen]{': ' + caption if caption else ''}"
            elif tipo in ("document", "file"):
                # El cliente envió un archivo (PDF, AI, etc.)
                nombre = msg.get("document", {}).get("filename", "archivo")
                texto = f"[ARTE RECIBIDO - documento: {nombre}]"
            else:
                # Otro tipo (audio, video, sticker) — ignorar
                continue

            mensajes.append(MensajeEntrante(
                # Normalizar el chat_id para que sea consistente entre mensajes
                # entrantes y salientes (Whapi puede enviar "521234@c.us" o "521234")
                telefono=_normalizar_telefono(msg.get("chat_id", "")),
                texto=texto,
                mensaje_id=msg.get("id", ""),
                es_propio=msg.get("from_me", False),
                # Si el mensaje es un reply, Whapi incluye el ID del mensaje citado
                quoted_id=msg.get("context", {}).get("quoted_id", ""),
            ))
        return mensajes

    async def eliminar_mensaje(self, mensaje_id: str) -> bool:
        """
        Borra un mensaje del chat via Whapi API.
        Se usa para que los comandos de operador (#abdiel, #grace, #bot)
        no queden visibles en el chat del cliente.
        """
        if not self.token or not mensaje_id:
            return False
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{self.url_base}/messages/{mensaje_id}",
                headers=headers,
            )
            if r.status_code not in (200, 204):
                logger.warning(f"No se pudo eliminar mensaje {mensaje_id}: {r.status_code} — {r.text}")
                return False
            logger.info(f"Mensaje de comando eliminado del chat: {mensaje_id}")
            return True

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Whapi.cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado — mensaje no enviado")
            return False
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self.url_envio,
                json={"to": telefono, "body": mensaje},
                headers=headers,
            )
            if r.status_code != 200:
                logger.error(f"Error Whapi: {r.status_code} — {r.text}")
            return r.status_code == 200
