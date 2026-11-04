# Prometheus metrics through mqtt2prometheus

The gateway has no listener of its own ([ADR 0015](../../docs/decisions/0015-provider-neutral-documents-on-the-bus.md)):
an exporter beside it reads its documents from the broker and serves them to Prometheus. These
are configurations for [mqtt2prometheus](https://github.com/hikhvar/mqtt2prometheus) (EXT-013 of
the review of 2026-11), which reads one topic path per instance:

| File | Reads | Metrics |
| --- | --- | --- |
| [`gateway.yaml`](gateway.yaml) | `state` and `stats` | the radio's link, battery, airtime, queue, noise floor, packets, uptime; the gateway's health |
| [`contacts.yaml`](contacts.yaml) | `contacts/<key>` | each contact's battery, readings, signal and whether its telemetry answers are current |

The documents are described in [mqtt-api.md](../../docs/mqtt-api.md); the metrics' names and
help texts are in the files. Replace `meshcore/radio1` with your `base_topic` and `node_id`.
`tests/test_contrib.py` checks that every field these files read is one the gateway's documents
have, and that every value of a mapped field has a number.

## Running them

Give the exporters a broker user of their own that may only read
([mqtt-api.md](../../docs/mqtt-api.md#securing-the-broker)):

```text
user mqtt2prometheus
topic read meshcore/radio1/state
topic read meshcore/radio1/stats
topic read meshcore/radio1/contacts/+
```

With that ACL the broker also keeps the other documents and events away from `gateway.yaml`'s
subscription to `meshcore/radio1/+`. Then, with the release of mqtt2prometheus you checked:

```sh
export MQTT2PROM_MQTT_USER=mqtt2prometheus MQTT2PROM_MQTT_PASSWORD=...
mqtt2prometheus -config gateway.yaml -listen-port 9641 &
mqtt2prometheus -config contacts.yaml -listen-port 9642 &
```

and in `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: meshcore-gateway
    static_configs:
      - targets: ["127.0.0.1:9641", "127.0.0.1:9642"]
```

`state` and `stats` come every 60 s with their time, so a stopped gateway drops out of the
graphs after Prometheus' five minutes. A contact's document comes when something about the
contact changes: its metrics take the scrape's time and hold until the next document or the
cache timeout of two hours; `meshcore_contact_answering` says whether its telemetry is current.
mqtt2prometheus speaks MQTT 3.1.1, which the broker serves beside the gateway's `[mqtt] protocol`.

## Privacy

`contacts.yaml` labels each metric with the contact's full public key, and Prometheus keeps the
readings for its retention time, beyond `[history] readings_days` and beyond an exclusion with
`--forget`. Positions are left out on purpose: a time series of latitudes is a movement profile.
Export only the contacts' documents your household agreed to, or leave `contacts.yaml` out.
