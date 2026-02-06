# kenergy

Tools for analyzing macOS power metrics.

## Install

```bash
ln -s $(pwd)/kenergy.sh ~/.local/bin/kenergy
```

## Usage

```bash
# Collect power metrics
kenergy watch

# Analyze today's power data
kenergy analyze

# Analyze power data for a specific date
kenergy analyze 2026-01-15
```

## License

MIT
