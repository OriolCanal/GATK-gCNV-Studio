Definición de la app
Quiero generar una app para poder generar cohortes de GATK gCNV. La estructura es que el usuario crea un proyecto subiendo un bed, una vez analizado el bed, el usuario debe poder subir bams analizados con el bed y poder ver métricas para ver la calidad del bam. Una vez subidos los bams, el usuario debe pòder generar cohortes seleccionando las muestras que quiere utilizar por cada cohorte.

La aplicación debe estar creada con flask jinja y javascript.

estructura:
Las distintas entidades deben estar guardadas en la base de datos: Los beds, paths, paths de outputs donde se obtiene la información, los parámetros de GATK gCNV utilizados para cada análisis, los bams que se han asignado a cada bed, cada muestra con sus bams y los outputs específicos de muestra, los modelos de cohortes que se han generado dentro de cada bed con las muestras que forman cada cohorte, los parámetros del gatk gcnv utilizado en la creación del modelo y lo que consideres necesario guardar en base de datos.

Storage:
El storage debe ser en un directorio indicado en el config que el default debe ser /ngs-results/GATK-gCNV-models/. Aquí debe haber una carpeta por cada bed, dentro de cada bed debe haber una carpeta donde se guarda los output de los análisis del bed, otra carpeta donde se guardan los bams y dentro la carpeta de los bams, un directorio donde haya los outputs de los análisis de los bams. Después debe haber una carpeta dentro de la carpeta de bed para cada modelo creado donde dentro haya todos los outputs del modelo generado.

