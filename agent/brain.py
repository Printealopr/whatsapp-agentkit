# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.
Soporta tool use: Printealito puede notificar al equipo cuando un cliente
necesita asistencia humana.
"""

import os
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Modelo de Claude a utilizar (configurable vía .env)
MODELO_CLAUDE = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Herramientas disponibles para Printealito
HERRAMIENTAS = [
    {
        "name": "notificar_equipo",
        "description": (
            "Notifica al equipo de Printealo cuando un cliente necesita asistencia humana. "
            "Úsala cuando: el cliente pregunta por el estatus de una orden existente, "
            "pide hablar con alguien del equipo, o la situación requiere intervención humana "
            "(órdenes rush de camisas, situaciones especiales, etc.). "
            "La herramienta verifica el horario automáticamente y envía el aviso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resumen": {
                    "type": "string",
                    "description": "Resumen breve de lo que el cliente necesita (máximo 120 caracteres)"
                }
            },
            "required": ["resumen"]
        }
    }
]


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres Printealito, el asistente virtual de Printealo. Responde en español.")


def obtener_mensaje_error() -> str:
    """Retorna el mensaje de error configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo un problemita técnico. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    """Retorna el mensaje de fallback configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí bien tu mensaje. ¿Me puedes contar qué necesitas? 😊")


def _serializar_contenido(content_blocks) -> list:
    """
    Convierte los bloques de contenido de la respuesta de Claude a formato dict
    para poder incluirlos en el historial de mensajes de la siguiente llamada.
    """
    resultado = []
    for bloque in content_blocks:
        if bloque.type == "text":
            resultado.append({"type": "text", "text": bloque.text})
        elif bloque.type == "tool_use":
            resultado.append({
                "type": "tool_use",
                "id": bloque.id,
                "name": bloque.name,
                "input": bloque.input,
            })
    return resultado


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "") -> str:
    """
    Genera una respuesta usando Claude API.
    Soporta tool use: si Claude necesita notificar al equipo, ejecuta la herramienta
    y continúa la conversación con el resultado.

    Args:
        mensaje:   El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        telefono:  Número del cliente (para incluirlo en la notificación al equipo)

    Returns:
        La respuesta generada por Claude para enviarle al cliente
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Construir lista de mensajes
    mensajes = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in historial
    ]
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model=MODELO_CLAUDE,
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes,
            tools=HERRAMIENTAS,
        )

        # ── Claude quiere usar una herramienta ────────────────────────────
        if response.stop_reason == "tool_use":
            # Encontrar el bloque tool_use en la respuesta
            tool_block = next(b for b in response.content if b.type == "tool_use")

            # Ejecutar la herramienta solicitada
            if tool_block.name == "notificar_equipo":
                from agent.tools import notificar_equipo
                resultado = await notificar_equipo(
                    telefono_cliente=telefono,
                    resumen=tool_block.input.get("resumen", ""),
                )
            else:
                resultado = "herramienta_no_encontrada"

            logger.info(f"Tool use: {tool_block.name} → {resultado}")

            # Agregar la respuesta del asistente (con el tool_use) al historial
            mensajes.append({
                "role": "assistant",
                "content": _serializar_contenido(response.content),
            })

            # Agregar el resultado de la herramienta como mensaje del usuario
            mensajes.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": resultado,
                }],
            })

            # Segunda llamada: Claude genera la respuesta final para el cliente
            response = await client.messages.create(
                model=MODELO_CLAUDE,
                max_tokens=1024,
                system=system_prompt,
                messages=mensajes,
                tools=HERRAMIENTAS,
            )

        # ── Extraer texto de la respuesta final ───────────────────────────
        texto_blocks = [b for b in response.content if b.type == "text"]
        if not texto_blocks:
            return obtener_mensaje_error()

        respuesta = texto_blocks[0].text
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
