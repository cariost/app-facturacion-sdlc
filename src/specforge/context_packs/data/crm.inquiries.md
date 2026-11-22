+++
pack_code = "CRM.inquiries"
module = "CRM"
title = "CRM: atención automatizada de consultas y cotizaciones"
+++
Canal automatizado para consultas, solicitudes de cotización y agendamiento, agnóstico
al rubro.

## Flujo
1. Ingreso por canal (web, email, mensajería) con identificación del cliente si es posible.
2. Clasificación agéntica: intención (consulta, cotización, agendamiento, reclamo, otro),
   entidades mencionadas (activo, producto, fecha) y urgencia.
3. Registro determinista: se crea la interacción vinculada a cliente y activo con la
   clasificación propuesta y su confianza.
4. Respuesta: el agente redacta con información de fuentes autorizadas (catálogo, FAQ del
   tenant, disponibilidad). Precios, plazos y stock se consultan al plano determinista.
5. Derivación a un asesor según reglas deterministas.

## Reglas de derivación (parámetros)
- `inquiries.handoff_after_turns`: turnos automáticos máximos antes de derivar.
- `inquiries.min_confidence`: bajo este umbral de confianza se deriva.
- `inquiries.always_handoff_intents`: intenciones que siempre van a un humano (p. ej.
  reclamos).
- `inquiries.sla.first_response`: tiempo máximo de primera respuesta; su vencimiento escala.

## Cotizaciones
- El agente arma un borrador de cotización con ítems del catálogo; el cálculo de precio,
  descuentos e impuestos es determinista.
- Una cotización tiene vigencia configurable (`inquiries.quote.validity`) y estados
  (BORRADOR, ENVIADA, ACEPTADA, VENCIDA, RECHAZADA).

## Excepciones
- Cliente no identificado: se registra como prospecto y se pide el dato mínimo configurado.
- Contenido sensible o fuera de alcance: no se responde automáticamente; se deriva.
