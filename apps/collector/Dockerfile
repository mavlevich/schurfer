FROM golang:1.26.6-alpine AS builder
WORKDIR /app
COPY apps/collector/go.mod apps/collector/go.sum ./
RUN go mod download
COPY apps/collector/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o /collector ./cmd/collector
RUN CGO_ENABLED=0 GOOS=linux go build -o /hotset ./cmd/hotset
RUN CGO_ENABLED=0 GOOS=linux go build -o /orderflow ./cmd/orderflow
RUN CGO_ENABLED=0 GOOS=linux go build -o /momentum-capture ./cmd/momentumcapture
RUN CGO_ENABLED=0 GOOS=linux go build -o /momentum-capture-binance ./cmd/momentumcapturebinance
RUN CGO_ENABLED=0 GOOS=linux go build -o /liquidation-capture ./cmd/liquidationcapture

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /collector /collector
COPY --from=builder /hotset /hotset
COPY --from=builder /orderflow /orderflow
COPY --from=builder /momentum-capture /momentum-capture
COPY --from=builder /momentum-capture-binance /momentum-capture-binance
COPY --from=builder /liquidation-capture /liquidation-capture
ENTRYPOINT ["/collector"]
