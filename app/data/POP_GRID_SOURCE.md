# Population Grid Data Source

## Asset
`pop_grid.csv` — globales Bevölkerungsdichte-Gitter, 0,25-Grad-Auflösung

## Quelldaten
**Produkt:** GHS_WUP_POP_E2025_GLOBE_R2025A_54009_1000_V1_0  
**Quelle:** GHSL — Global Human Settlement Layer, GHS-POP (WUP-Projektion)  
**Herausgeber:** European Commission, Joint Research Centre (EC JRC) & Columbia University CIESIN  
**Lizenz:** CC-BY-4.0  
**Bezugsquelle:** https://ghsl.jrc.ec.europa.eu  
**Epoche:** 2025  
**Originale Auflösung:** 1 km × 1 km, Koordinatensystem ESRI:54009 (Mollweide)  

## Aggregationsverfahren

1. **Dezimierung:** Das Raster (18 000 × 36 082 Pixel, ~648 Mio. Zellen) wird mit `rasterio.read(out_shape, resampling=Resampling.average)` auf Faktor 1/4 gelesen (effektive Zellgröße ~4 km). Das ergibt ca. 4 500 × 9 021 = 40,6 Mio. dezimierte Pixel. Der Faktor 4 wurde gewählt, damit die Quellzellen (~4 km) deutlich feiner als die Ziel-Bins (0,25° ≈ 28 km) sind und keine Auflösung verloren geht.

2. **Reprojizierung:** Jedes dezimierte Pixel wird aus Mollweide (ESRI:54009) nach WGS84 (EPSG:4326) umgerechnet — mittels `pyproj.Transformer`.

3. **Binning:** Jeder Pixelwert (Durchschnitt der Originalpixel im Block × 16 = Blockgesamtbevölkerung) wird in eine 0,25°×0,25°-Zelle (Lat/Lon) eingeordnet; alle Blockwerte einer Zelle werden summiert. Zellenmitten liegen auf Vielfachen von 0,25° + 0,125° (z. B. 0,125°, 0,375° …), gerundet auf 2 Nachkommastellen.

4. **Filter:** Zellen mit Bevölkerung < 1 (Ozeane, unbewohnte Gebiete) werden verworfen.

## Ergebnis (Stand Erstellung)
- **Auflösung:** 0,25° × 0,25° Lat/Lon-Zellen  
- **Anzahl Zellen:** 163 523  
- **Bevölkerungssumme:** ~8,23 Mrd. (plausibel für Weltbevölkerung 2025)  
- **CSV-Größe:** ~2,87 MB  

## Attribution (Pflichtangabe gemäß CC-BY-4.0)
> Daten: GHSL GHS-POP (WUP-Projektion), Produkt GHS_WUP_POP_E2025_GLOBE_R2025A_54009_1000_V1_0, EC JRC & CIESIN, CC-BY-4.0, Quelle ghsl.jrc.ec.europa.eu
