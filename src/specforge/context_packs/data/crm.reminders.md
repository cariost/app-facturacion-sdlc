+++
pack_code = "CRM.reminders"
module = "CRM"
title = "CRM: políticas de recordatorio por tiempo, uso o ciclo"
+++
Un recordatorio es el resultado de evaluar una política sobre un activo. La política es
configuración del tenant; el motor que la evalúa es determinista e idéntico para todo giro.

## Tipos de política
- TIME: vence cuando `ahora >= último_servicio + intervalo`. Parámetro
  `reminders.time.interval` (duración ISO 8601, p. ej. P6M).
- USAGE: vence cuando `contador_actual - contador_en_último_servicio >= umbral`. Parámetros
  `reminders.usage.counter` (nombre del contador) y `reminders.usage.threshold`.
- CYCLE: vence en fechas de calendario recurrentes (aniversario, cada N meses en el día D,
  fin de periodo). Parámetro `reminders.cycle.rule` (regla de recurrencia tipo RRULE).
- COMBINED: compone políticas con `reminders.combine` = FIRST_OF (la primera que vence)
  o ALL_OF (todas deben vencer).

## Anticipación, repetición y cierre
- `reminders.lead_time`: cuánto antes del vencimiento se avisa.
- `reminders.repeat.max` y `reminders.repeat.every`: recordatorios de seguimiento si el
  cliente no responde; al llegar al máximo se deriva a un asesor.
- Un nuevo evento de servicio sobre el activo cierra el ciclo y reinicia la evaluación.

## Idempotencia y trazabilidad
- Un solo recordatorio por (activo, política, ciclo): clave
  `tenant:asset:policy:cycle_key`. Reevaluar no duplica.
- Cada evaluación registra entradas (fechas, lecturas), parámetros resueltos con su
  `config_version` y resultado (vence / no vence / omitido con motivo).

## Excepciones habituales
- Consentimiento revocado o canal sin consentimiento: no se envía, se registra la omisión.
- Lectura de contador ausente o que retrocede: no se infiere; se marca "dato insuficiente"
  y, si el perfil lo indica, se cae a la política TIME.
- Activo dado de baja o cliente inactivo: la política se suspende.

## Lo que hace el agente y lo que no
- Agente: redactar el mensaje según plantilla y tono del tenant; resumir el historial del
  activo para el asesor.
- Determinista: decidir si vence, cuándo, por qué canal y si se envía.
