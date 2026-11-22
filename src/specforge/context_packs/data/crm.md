+++
pack_code = "CRM"
module = "CRM"
title = "CRM: modelo base configurable por giro"
+++
CRM agnóstico al rubro. El vocabulario del giro (qué es un "activo", qué es un "servicio")
llega desde el perfil de negocio, no desde el código.

## Entidades base
- Cliente (`party`): persona natural o jurídica, con documento de identidad validado.
- Contacto: medios de contacto por canal (email, teléfono, mensajería) con preferencia.
- Activo del cliente (`customer_asset`): lo que el negocio atiende de forma recurrente
  (un equipo, una suscripción, una instalación). Tiene `asset_type` configurable y
  atributos libres validados por un esquema del perfil.
- Evento de servicio: registro fechado de una atención sobre un activo, con lecturas de
  contadores opcionales.
- Interacción: cualquier contacto entrante o saliente, vinculado a cliente y opcionalmente
  a un activo.

## Consentimiento y canales
- El consentimiento de contacto es por cliente y canal, con fecha y origen. Sin
  consentimiento vigente no hay mensajes salientes; la omisión se registra.
- Horario permitido de contacto (`crm.contact.quiet_hours`) y canal preferido son
  parámetros del perfil.
- Las plantillas de mensaje son configurables por tenant e idioma; el agente puede redactar
  variantes, pero el envío lo dispara una regla determinista.

## Integración con otros módulos
- Una cotización aceptada genera un documento en CORE/SUNAT: el precio y los impuestos los
  calcula el plano determinista a partir del catálogo, nunca el agente.
- Los eventos de servicio pueden originar comprobantes electrónicos (SUNAT) y registros
  (SIRE) a través de sus propios módulos.
