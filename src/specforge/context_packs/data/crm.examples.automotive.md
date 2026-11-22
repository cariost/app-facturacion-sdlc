+++
pack_code = "CRM.examples.automotive"
module = "CRM"
title = "CRM: ejemplo de configuración para un taller automotriz"
industry = "automotriz"
+++
Overlay de giro. Solo se carga para perfiles con `industry = "automotriz"`. Muestra cómo se
configuran las políticas genéricas; no introduce reglas nuevas.

## Mantenimiento periódico como configuración
- `asset_type`: vehículo, con atributos placa, marca, modelo, año y VIN.
- Política COMBINED con `reminders.combine = FIRST_OF`:
  - TIME con `reminders.time.interval = P6M`.
  - USAGE con `reminders.usage.counter = "odometro_km"` y
    `reminders.usage.threshold = 5000`.
- `reminders.lead_time = P7D` y canal preferido mensajería.

## Consultas típicas
- Cotización de mantenimiento por kilometraje, disponibilidad de turnos y estado del
  vehículo en taller. `inquiries.handoff_after_turns = 3`.
