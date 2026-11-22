+++
pack_code = "CORE"
module = "CORE"
title = "CORE: principios transversales del ERP"
+++
Contexto común a todos los módulos. Ningún módulo puede contradecirlo.

## Multi-tenant y perfiles de negocio
- Toda entidad de negocio lleva `tenant_id`; ninguna consulta cruza tenants.
- El giro del negocio se describe en `business_profiles` (tenant_id, code, industry, config).
- `config` es un documento versionado (`config_version`): las reglas leen parámetros de ahí,
  nunca constantes del giro escritas en código.
- Resolución de parámetros: perfil del tenant > overlay de la industria > valor por defecto
  declarado en la historia (`ConfigurableParameter.default`). Un parámetro sin valor ni
  default es un error de configuración explícito, no un comportamiento implícito.

## Separación de planos
- Plano determinista (código + PostgreSQL): cálculos fiscales, montos, impuestos, estados,
  numeración, vencimientos, validaciones y transiciones. Mismo input, mismo output.
- Plano agéntico (LLM): clasificar, redactar, resumir, proponer. Nunca decide un monto,
  un estado fiscal ni una transición; propone y el plano determinista valida.
- Toda regla `RD-xx` de una historia se implementa en un componente DETERMINISTIC.

## Dinero, fechas e identificadores
- Montos con `Decimal` (nunca float), moneda ISO 4217 explícita y redondeo declarado
  (HALF_UP a 2 decimales salvo que la norma indique otro).
- Fechas en UTC en persistencia; la zona horaria del tenant solo para presentación y para
  calcular vencimientos de calendario.
- Identificadores de negocio con formato validado (p. ej. RUC de 11 dígitos con dígito
  verificador módulo 11, DNI de 8 dígitos).

## Auditoría e idempotencia
- Operaciones con efecto externo (envíos a SUNAT, notificaciones, cobros) llevan una clave
  de idempotencia derivada del negocio (p. ej. tenant + documento + intento).
- Los registros fiscales y de auditoría son append-only: se corrigen con un registro nuevo
  que referencia al anterior, nunca con UPDATE.
- Cada cambio de estado registra quién, cuándo, desde qué estado y por qué.

## Extensibilidad por configuración
- Un nuevo giro se habilita con un perfil y, si hace falta, un overlay de Context Pack;
  no con ramas de código por industria (`if industry == ...` está prohibido).
- Los catálogos (tipos de documento, canales, políticas) son tablas o enums versionados.
