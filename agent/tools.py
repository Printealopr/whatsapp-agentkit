# agent/tools.py — Herramientas del agente Printealito
# Generado por AgentKit

"""
Herramientas específicas de Printealo.
Funciones para FAQ, calificación de leads y toma de pedidos.
"""

import os
import yaml
import logging
import httpx
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("agentkit")

# Puerto Rico: AST (UTC-4), sin horario de verano
PR_TZ = timezone(timedelta(hours=-4))

# Número de Grace para notificaciones al equipo
NUMERO_NOTIFICACIONES = os.getenv("NUMERO_NOTIFICACIONES", "17879519388")


async def notificar_equipo(telefono_cliente: str, resumen: str) -> str:
    """
    Notifica al equipo de Printealo cuando un cliente necesita asistencia humana.
    Verifica si es horario laborable (lunes a viernes 9am-5pm hora de PR).

    Retorna:
        "equipo_notificado" — WhatsApp enviado a Grace exitosamente
        "fuera_de_horario"  — fuera del horario laboral, no se envió
        "error"             — fallo al enviar la notificación
    """
    ahora = datetime.now(PR_TZ)
    es_dia_laborable = ahora.weekday() < 5   # 0=lunes, 4=viernes
    es_horario = 9 <= ahora.hour < 17        # 9am a 5pm

    if not (es_dia_laborable and es_horario):
        logger.info(f"Notificacion fuera de horario ({ahora.strftime('%A %H:%M')} PR) — no enviada")
        return "fuera_de_horario"

    token = os.getenv("WHAPI_TOKEN")
    if not token:
        logger.error("WHAPI_TOKEN no configurado — no se pudo notificar al equipo")
        return "error"

    mensaje_equipo = (
        f"⚠️ Cliente {telefono_cliente} necesita asistencia. "
        f"Está preguntando por: {resumen}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://gate.whapi.cloud/messages/text",
            json={"to": NUMERO_NOTIFICACIONES, "body": mensaje_equipo},
            headers=headers,
        )
        if r.status_code == 200:
            # Guardar el ID del mensaje enviado a Grace: cuando ella responda
            # citando esa notificación, sabremos a qué cliente reenviar la respuesta
            mensaje_id = r.json().get("message", {}).get("id", "")
            if mensaje_id:
                from agent.memory import guardar_notificacion
                await guardar_notificacion(mensaje_id, telefono_cliente, resumen)
            else:
                logger.warning("Whapi no retornó ID del mensaje — la respuesta del equipo no podrá vincularse")
            logger.info(f"Equipo notificado sobre cliente {telefono_cliente}: {resumen}")
            return "equipo_notificado"
        else:
            logger.error(f"Error enviando notificacion al equipo: {r.status_code} — {r.text}")
            return "error"


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención y si está abierto ahora."""
    info = cargar_info_negocio()
    horario = info.get("negocio", {}).get("horario", "Lunes a viernes 9am-5pm")

    # Verificar si está abierto según hora actual (Puerto Rico = UTC-4)
    ahora = datetime.utcnow()
    hora_pr = ahora.hour - 4  # Ajuste simplificado a hora de PR
    dia_semana = ahora.weekday()  # 0=lunes, 6=domingo

    esta_abierto = (
        dia_semana < 5 and  # Lunes a viernes
        9 <= hora_pr < 17   # 9am a 5pm
    )

    return {
        "horario": horario,
        "esta_abierto": esta_abierto,
        "mensaje": "Estamos disponibles ahora mismo 😊" if esta_abierto
                   else "En este momento estamos fuera de horario. Lunes a viernes 9am-5pm."
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ── Calificación de leads ────────────────────────────────────

def crear_lead(telefono: str, nombre: str, producto: str, cantidad: int,
               tiene_diseno: bool, fecha_deseada: str) -> dict:
    """
    Registra un lead calificado de Printealo.

    Args:
        telefono: Número del cliente
        nombre: Nombre del cliente o empresa
        producto: Producto solicitado (camisas, banners, etc.)
        cantidad: Cantidad de piezas
        tiene_diseno: Si el cliente ya tiene diseño listo
        fecha_deseada: Fecha de entrega que el cliente necesita

    Returns:
        Diccionario con el lead registrado
    """
    lead = {
        "telefono": telefono,
        "nombre": nombre,
        "producto": producto,
        "cantidad": cantidad,
        "tiene_diseno": tiene_diseno,
        "fecha_deseada": fecha_deseada,
        "fecha_registro": datetime.utcnow().isoformat(),
        "estado": "nuevo",
    }
    logger.info(f"Lead registrado: {nombre} — {producto} x{cantidad}")
    # TODO: guardar en base de datos o enviar a CRM
    return lead


# ── Toma de pedidos ──────────────────────────────────────────

def calcular_tecnica_recomendada(producto: str, cantidad: int, colores: int) -> str:
    """
    Sugiere la técnica de impresión más adecuada según el pedido.

    Args:
        producto: Tipo de producto (camisa, banner, taza, etc.)
        cantidad: Número de piezas
        colores: Número de colores en el diseño (0 = full color)

    Returns:
        Nombre de la técnica recomendada con explicación
    """
    producto_lower = producto.lower()

    if "taza" in producto_lower or "tumbler" in producto_lower:
        return "Sublimación — ideal para tazas y tumblers, full color de alta calidad"

    if "gorra" in producto_lower:
        return "Bordado — profesional y duradero para gorras"

    if "banner" in producto_lower or "rotulo" in producto_lower or "vinilo" in producto_lower:
        return "Impresión digital de gran formato — colores vibrantes y resistente al clima"

    if "camisa" in producto_lower or "uniforme" in producto_lower:
        if colores == 0 or colores > 4:
            return "DTF (Direct to Film) — perfecto para diseños full color en camisas, sin mínimo de piezas"
        elif cantidad >= 24:
            return "Serigrafía — más económico para grandes cantidades con pocos colores"
        else:
            return "DTF (Direct to Film) — ideal para cantidades pequeñas con diseños detallados"

    return "DTF o Serigrafía — contáctanos para recomendarte la mejor opción según tu diseño"


def confirmar_pedido(nombre: str, producto: str, cantidad: int,
                     detalles_diseno: str, fecha_entrega: str, contacto: str) -> str:
    """
    Genera el resumen de confirmación de un pedido.

    Returns:
        Mensaje de confirmación formateado para enviar al cliente
    """
    tecnica = calcular_tecnica_recomendada(producto, cantidad, 0)

    resumen = f"""✅ *Pedido registrado en Printealo*

📦 Producto: {producto}
🔢 Cantidad: {cantidad} piezas
🎨 Diseño: {detalles_diseno}
⚙️ Técnica sugerida: {tecnica}
📅 Fecha deseada: {fecha_entrega}
👤 Cliente: {nombre}
📞 Contacto: {contacto}

Nuestro equipo revisará tu pedido y te enviará la cotización formal pronto.
¡Gracias por elegir Printealo! 🖨️"""

    logger.info(f"Pedido confirmado para {nombre}: {producto} x{cantidad}")
    return resumen
