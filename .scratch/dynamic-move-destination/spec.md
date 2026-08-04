# Dynamic move/copy destination using capture groups

Move and copy actions must support regex capture groups (`\1`, `\g<name>`,
`condition.\g<name>`) in the `destination` field so that items can be routed
into dynamically-named subdirectories.

## Motivating example

```yaml
rules:
  - name: Route tagged archives
    conditions:
      is_archive:
        field: file_name
        pattern: '\.(zip|rar|7z)$'
      tag:
        field: file_name
        pattern: '\[(?P<category>[^\]]+)\]'
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: '../\g<category>'
```

Given `report [finance].zip`, this should move the file to
`../finance/report [finance].zip`, creating the `finance/` folder if needed.
