# Anexo: uso de IA generativa

Este anexo acompaña la [declaración sobre la contribución de la IA](../README.md#declaración-sobre-la-contribución-de-la-ia) del README. Sigue la guía de la Biblioteca PUCP sobre APA 7 e inteligencia artificial ([casos de uso](https://guiastematicas.biblioteca.pucp.edu.pe/apa_7aedicion/IA_casosdeuso)), que recomienda registrar los prompts para que el trabajo sea transparente y reproducible.

- **Herramienta:** Claude Code (Anthropic, 2026), con el modelo Claude Opus 5.5.
- **Fechas de uso:** 21 y 22 de octubre de 2026.
- **Cómo se trabajó:** Claude Code funciona en la terminal. Para cada pedido proponía un plan o un borrador, y yo lo aprobaba o lo corregía. Recién después escribía el código en este repositorio y lo ejecutaba en mi computadora. Las cifras del trabajo salen de ese código ejecutado sobre datos abiertos, no de texto generado por el modelo.

## Prompts del autor

Los prompts provienen de la sesión de trabajo y están en el orden en que los escribí. Algunos están resumidos o reformulados para mayor claridad, y se omitieron confirmaciones breves y pedidos que no afectan el contenido del trabajo. Cuando la herramienta ofrecía opciones para elegir, se indica la opción elegida. Las respuestas de la IA no se incluyen: su resultado está en el código, en el README y en el historial de commits.

### 1. Planificación y entrega del trabajo

**Prompt 1.** Pedido inicial:

```text
planifiquemos una mapa de visualizacion de los incendios que estan ocurriendo actualmente en peru hasta el dia de hoy 21 de octubre de 2026. quiero usar los datos de distritos de este proyecto (porque ya tiene coordenadas) "https://github.com/d2cml-ai/Data-Science-Python/blob/main/sessions/07-raster/lecture/raster_aplicado.ipynb", "https://github.com/d2cml-ai/Data-Science-Python", tambien quiero usar los natos de incendios de nasa firms (tengo cuenta), tambien quiero usar "https://sites.research.google/gr/open-buildings/".

estas son las instrucciones: "https://claude.ai/artifact/7PDjxhAxnnJV2K24rVKWT6" (visualizalo en el navegador). usa worldpop.

el objetivo es hacer storytelling como este trabajo: "https://claude.ai/artifact/UsyGgMV7ysFBEDzzrW5DeU". Debe ser creativo y visualmente bonito. el objetivo final tambien es compartirlo como un artifact asi como este ultimo link.

ayudame a elaborar el plan
```

**Prompt 2.** Elección del concepto entre tres opciones (historia con scroll, tablero mejorado, explorador 3D):

```text
Historia scrollytelling (Recomendado)
```

**Prompt 3.** Respuesta a la pregunta sobre cómo crear el repositorio:

```text
publico. commits descriptivos. readme con mi nombre Rodrigo Norabuena
```

**Prompt 4.**

```text
al final quiero que me ayudes a completar esto:

Nombre:
Reto (A–F o propio):
Link de tu web en Claude:
Repo de GitHub:

es el formato de entrega
```

### 2. Autoría, cita y licencias

**Prompt 5.**

```text
actualiza el artifact para que al final exista referencia de autoria de las fuentes usadas.

muestrame un borrador antes de hacer cambios
```

### 3. Monitor en vivo, modo película y celular

**Prompt 6.**

```text
me gustaria que pudiera actualizarse en tiempo real o con una actualizacion frecuente. qué cambios tendrian que ocurrir? es viable? dame un diagnostico de esta solicitud y un plan de implementacion si se pudiera
```

**Prompt 7.**

```text
si, estoy de acuerdo. mejor es tener un apartado separado para el monitoreo en vivo. este debe ser accesible desde el artifact de claude y debe ser una segunda implementacion para que el original que tendria tag v1.0.0 se mantenga preservado tambien en el repositorio. tambien me gustaria implementar una opcion de un boton que te permita tener una transcicion automatica. como dejandote avanzar en la historia que cuenta la pagina web. esta funcionalidad estaria inspirada del boton de "Ver como pelicula" de este sitio web: "https://margarethamilton.dev/", "https://github.com/midudev/margarethamilton.dev". tambien podemos inspirarnos de este repo para mejorar el scrollytelling
```

**Prompt 8.**

```text
si, hazlo. tambien hay que mejorar la interaccion desde un navegador de movil porque mucho se usa celulares para revisar la informacion. quiza el repo que te acabo de compartir puede ayudar en esto
```

### 4. Declaración de uso de IA

**Prompt 9.**

```text
el readme necesita una apartado sobre el uso de ia: https://guiastematicas.biblioteca.pucp.edu.pe/apa_7aedicion/IA_casosdeuso
veamos como deberia ser ese apartado
```

## Referencia

Anthropic. (2026). *Claude Code* (Claude Opus 5.5) [Modelo de lenguaje grande]. https://claude.com/claude-code
