README phase1

Quiero desarrollar una aplicación web para poder construir modelos de GATK gCNV en paneles y exomas.

Hay distintos puntos que deben cumplirse durante toda la aplicación:
- los análisis deben correrse con nextflow
- todos los comandos de GATK gCNV el usuario debe poder escoger el valor de los parámetros
- Estos parámetros deben quedarse guardados en archivos de configuración
Este proyecto debe tener distintas capas:

Primero el usuario debe subir un bed (de panel o exoma) y escoger el genoma de referencia (Grch37 o Grch38). Una vez subido quiero que haya una validación del bed. Una vez validado la web lanza un job para analizar el bed: se analiza usando gakt processintervals, GATK annotate intervals . Aqui el usuario debe poder visualizar distintas metricas obtenidos del bed:    Visualizaciones
A. Distribución de longitudes

Histograma de longitud de intervalos.

Esto permite detectar:
- Intervalos extremadamente pequeños.
- Intervalos excesivamente grandes.
- Distribuciones heterogéneas.
- Diferencias entre paneles.
B. Distribución de intervalos por cromosoma

Un gráfico de barras con el número de intervalos por cromosoma.

C. Comparación BED original vs BED procesado

Una tabla comparativa:
Métrica	Original	Processed
Number of intervals	25,000	22,400
Total bases	15 Mb	18 Mb
Mean interval length	600 bp	804 bp
Overlapping intervals	1,200	0


Los valores son ilustrativos.

Esta comparación es especialmente útil para detectar si el padding o la fusión de intervalos ha cambiado considerablemente el diseño original.  También debe haber un gráfico para le GC content donde en el eje X hay los intervalos ordenados por cromosoma y posición y en el eje Y el porcentaje GC. Debe ser interactivo, donde cuando pasas por encima de cada intervalo debe aparecer el intervalo, el gen y el porcentaje GC.

A. Mappability por intervalo

Con el mismo eje X utilizado para GC content.

B. Distribución de mappability

Histograma entre 0 y 1.

C. Relación GC vs mappability

Scatter plot donde cada punto representa un intervalo.

Esto permite identificar regiones que combinan características potencialmente problemáticas.

Hay un detalle importante: el track de Umap utilizado debe corresponder a la longitud de lectura apropiada para el experimento. GATK lo especifica expresamente en la documentación de AnnotateIntervals. gatk.broadinstitute.org

Por tanto, no dejaría únicamente una ruta de mappability por referencia en YAML. Añadiría también la configuración de read length del track.

Cómo organizaría la interfaz de resultados
Yo diseñaría una página dentro de cada proyecto BED con cinco secciones.
1. Overview
Tarjetas resumen:
- Original intervals.
- Processed intervals.
- Total genomic bases.
- Mean interval length.
- GC content median.
- Mappability median.
- Percentage of intervals retained after filtering.
2. Interval characteristics
Tres gráficos independientes, con el mismo eje X:

GC content

Mappability

Segmental duplication content

El eje X debe representar los intervalos ordenados por cromosoma y posición. Añadiría un selector de cromosoma para evitar que miles de intervalos hagan ilegible el gráfico.
3. Interval distributions
Histogramas de:
- Interval length.
- GC content.
- Mappability.
- Segmental duplication content.
Y un scatter plot GC vs mappability.
4. Filtering results
Un gráfico que represente el número de intervalos conservados y excluidos, acompañado de un desglose por motivo.

También una tabla descargable con las coordenadas y los motivos de exclusión.