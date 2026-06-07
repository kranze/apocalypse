# Identity Data — Source & License

## names.json

**Hand-curated** name pools per cultural cluster.  
Names were assembled from general cultural knowledge, publicly known naming
conventions per region, and widely-available name frequency lists (e.g.
Wikipedia's lists of most common given names and surnames by country).

No data was copied verbatim from a proprietary or restricted source.
These are common, widely-known names with no copyright claims.

**Clusters covered:** central_europe, eastern_europe, british_isles, nordics,
iberia, north_america, latin_america, south_asia, east_asia, southeast_asia,
east_africa, west_africa, middle_east, oceania, unknown.

## professions.json

**Hand-curated** global profession list with approximate frequency weights.  
Weights are rough estimates inspired by ILO (International Labour Organization)
employment-by-sector statistics and the UN/World Bank sectoral breakdowns.
They are deliberately approximate and are intended only for flavor/sampling, not
for demographic accuracy.

Reference: ILO World Employment and Social Outlook, public summaries
(https://www.ilo.org/global/research/global-reports/weso/). No data copied.

## age_pyramid.json

**Hand-curated** age bracket weights, loosely inspired by:
- UN World Population Prospects 2022 (global aggregate),
  https://population.un.org/wpp/ (CC-BY 3.0 IGO)

Weights are approximate and rounded for simplicity. Sufficient for
probabilistic sampling in a game context.

## Usage

All data is used exclusively for **procedural identity generation** in a
single-player game (Apocalypse Sim). No personal data is processed or stored.
The assets are fully offline and deterministic when used with a fixed RNG seed.
