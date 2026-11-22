+++
pack_code = "SIRE"
module = "SIRE"
title = "SIRE: registros electrónicos de ventas y compras"
+++
Sistema Integrado de Registros Electrónicos de SUNAT. Plano determinista; el agente puede
explicar diferencias de conciliación.

## Registros
- RVIE: Registro de Ventas e Ingresos Electrónico.
- RCE: Registro de Compras Electrónico.
- Periodo tributario mensual `AAAAMM`, por RUC del tenant.

## Propuesta y conciliación
- SUNAT genera una propuesta del registro a partir de los CPE informados. El ERP la
  descarga y la concilia contra sus propios comprobantes del periodo.
- Resultado por comprobante: coincide, falta en la propuesta, falta en el ERP o difiere
  en montos. Las diferencias se resuelven con ajustes o complementos antes de aceptar.
- La aceptación o reemplazo de la propuesta genera el registro del periodo; después solo
  se corrige mediante los mecanismos de ajuste posteriores previstos.

## Integración
- Comunicación por API SIRE con credenciales del tenant (`sire.api.*`) en un almacén de
  secretos; las operaciones largas devuelven ticket y se consultan de forma asíncrona.
- Cada descarga, envío y respuesta se guarda append-only con su ticket, periodo y hash.

## Reglas
- Un periodo cerrado no admite cambios en el ERP sin un ajuste registrado.
- La obligatoriedad y fechas de vencimiento por periodo son parámetros normativos
  (`sire.deadlines.*`) con vigencia, no constantes.
