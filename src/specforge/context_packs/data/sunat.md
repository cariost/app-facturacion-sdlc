+++
pack_code = "SUNAT"
module = "SUNAT"
title = "SUNAT: comprobantes de pago electrónicos (CPE)"
+++
Facturación electrónica en Perú. Todo el módulo es plano determinista; el agente solo
explica errores o redacta comunicaciones.

## Comprobantes y catálogos
- Tipos (catálogo 01): 01 factura, 03 boleta de venta, 07 nota de crédito, 08 nota de
  débito, 09 guía de remisión remitente, 20 retención, 40 percepción.
- Documento de identidad del adquiriente (catálogo 06), afectación del IGV (catálogo 07),
  tributos (catálogo 05) y motivos de notas (catálogos 09 y 10) se validan contra
  catálogos versionados, no contra listas en código.
- Serie de 4 caracteres (factura inicia con F, boleta con B) y correlativo numérico de hasta
  8 dígitos, único por emisor, tipo y serie; la numeración se asigna de forma atómica.

## Formato, firma y envío
- XML UBL 2.1 firmado con el certificado digital del emisor.
- Canal de envío configurable por tenant: SEE del contribuyente, OSE o SEE-SOL
  (`sunat.channel`), con credenciales en un almacén de secretos.
- La respuesta es la CDR (constancia de recepción): aceptado, aceptado con observaciones o
  rechazado. Se guarda el XML enviado, la CDR y el hash.
- Las boletas pueden informarse por resumen diario; las anulaciones de facturas por
  comunicación de baja; ambos procesos usan ticket asíncrono que se consulta luego.

## Montos e impuestos
- Totales recalculados desde las líneas: valor de venta, IGV, ISC, ICBPER, descuentos y
  cargos; el redondeo y la tasa vigente son parámetros normativos versionados
  (`tax.igv.rate`), nunca literales.
- Operaciones gravadas, exoneradas, inafectas y gratuitas se totalizan por separado.

## Plazos y estados
- Los plazos de envío y de baja son parámetros normativos (`sunat.deadlines.*`) con fecha
  de vigencia; el motor alerta antes del vencimiento.
- Estados del comprobante: BORRADOR, EMITIDO, ENVIADO, ACEPTADO, OBSERVADO, RECHAZADO,
  ANULADO. Un comprobante aceptado no se edita: se corrige con nota de crédito o débito.

## Reintentos
- Errores de transporte se reintentan con la misma clave de idempotencia; errores de
  validación de SUNAT no se reintentan: requieren corrección y, si aplica, nuevo número.
