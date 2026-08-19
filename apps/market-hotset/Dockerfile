FROM golang:1.26.6-alpine AS builder
WORKDIR /app
COPY go.work go.work.sum ./
COPY apps/market-hotset/ apps/market-hotset/
RUN cd apps/market-hotset && CGO_ENABLED=0 GOOS=linux go build -o /market-hotset ./cmd/market-hotset

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /market-hotset /market-hotset
ENTRYPOINT ["/market-hotset"]
