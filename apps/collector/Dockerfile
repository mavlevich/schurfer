FROM golang:1.26.5-alpine AS builder
WORKDIR /app
COPY apps/collector/go.mod apps/collector/go.sum ./
RUN go mod download
COPY apps/collector/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o /collector ./cmd/collector
RUN CGO_ENABLED=0 GOOS=linux go build -o /hotset ./cmd/hotset
RUN CGO_ENABLED=0 GOOS=linux go build -o /orderflow ./cmd/orderflow

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /collector /collector
COPY --from=builder /hotset /hotset
COPY --from=builder /orderflow /orderflow
ENTRYPOINT ["/collector"]
