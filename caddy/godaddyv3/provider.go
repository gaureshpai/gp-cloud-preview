package godaddyv3

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/caddyserver/caddy/v2"
	"github.com/caddyserver/caddy/v2/caddyconfig/caddyfile"
	"github.com/libdns/libdns"
)

const (
	apiBaseURL = "https://api.godaddy.com/v3/domains/zones"
	minimumTTL = 600 * time.Second
	maximumTTL = 24 * time.Hour
	pageSize   = 100
)

// Provider manages GoDaddy DNS TXT records through the authenticated v3 API.
type Provider struct {
	APIToken string `json:"api_token,omitempty"`

	baseURL string
	client  *http.Client
}

type record struct {
	ID   string `json:"recordId"`
	Type string `json:"type"`
	Name string `json:"name"`
	Data string `json:"data"`
	TTL  int    `json:"ttl"`
}

type recordsPage struct {
	Items []record `json:"items"`
}

type recordInput struct {
	Type string `json:"type"`
	Name string `json:"name"`
	Data string `json:"data"`
	TTL  int    `json:"ttl"`
}

func init() {
	caddy.RegisterModule(Provider{})
}

// CaddyModule registers this provider under the existing GoDaddy module name.
func (Provider) CaddyModule() caddy.ModuleInfo {
	return caddy.ModuleInfo{
		ID:  "dns.providers.godaddy",
		New: func() caddy.Module { return new(Provider) },
	}
}

// Provision resolves the configured PAT and prepares a bounded HTTP client.
func (p *Provider) Provision(ctx caddy.Context) error {
	p.APIToken = caddy.NewReplacer().ReplaceAll(p.APIToken, "")
	if strings.TrimSpace(p.APIToken) == "" {
		return errors.New("GoDaddy DNS PAT is required")
	}
	if p.baseURL == "" {
		p.baseURL = apiBaseURL
	}
	if p.client == nil {
		p.client = &http.Client{Timeout: 20 * time.Second}
	}
	return nil
}

// UnmarshalCaddyfile accepts `dns godaddy <pat>` or an api_token block.
func (p *Provider) UnmarshalCaddyfile(d *caddyfile.Dispenser) error {
	for d.Next() {
		if d.NextArg() {
			p.APIToken = d.Val()
		}
		if d.NextArg() {
			return d.ArgErr()
		}
		for nesting := d.Nesting(); d.NextBlock(nesting); {
			switch d.Val() {
			case "api_token":
				if p.APIToken != "" {
					return d.Err("api_token is already set")
				}
				if !d.NextArg() {
					return d.ArgErr()
				}
				p.APIToken = d.Val()
				if d.NextArg() {
					return d.ArgErr()
				}
			default:
				return d.Errf("unrecognized subdirective %q", d.Val())
			}
		}
	}
	if p.APIToken == "" {
		return d.Err("GoDaddy DNS PAT is required")
	}
	return nil
}

// GetRecords lists TXT records because this provider is used for ACME DNS challenges.
func (p *Provider) GetRecords(ctx context.Context, zone string) ([]libdns.Record, error) {
	zone, err := normalizeZone(zone)
	if err != nil {
		return nil, err
	}
	query := url.Values{"type": {"TXT"}}
	items, err := p.listRecords(ctx, zone, query)
	if err != nil {
		return nil, err
	}
	result := make([]libdns.Record, 0, len(items))
	for _, item := range items {
		if item.Type != "TXT" {
			continue
		}
		result = append(result, libdns.TXT{
			Name:         item.Name,
			Text:         item.Data,
			TTL:          time.Duration(item.TTL) * time.Second,
			ProviderData: item.ID,
		})
	}
	return result, nil
}

// AppendRecords creates individual TXT records without replacing neighboring values.
func (p *Provider) AppendRecords(ctx context.Context, zone string, records []libdns.Record) ([]libdns.Record, error) {
	zone, err := normalizeZone(zone)
	if err != nil {
		return nil, err
	}
	created := make([]libdns.Record, 0, len(records))
	for _, input := range records {
		rr := input.RR()
		if rr.Type != "TXT" {
			return p.rollbackCreated(ctx, zone, created, fmt.Errorf("GoDaddy v3 provider only supports TXT records, got %q", rr.Type))
		}
		name, err := relativeName(zone, rr.Name)
		if err != nil {
			return p.rollbackCreated(ctx, zone, created, err)
		}
		ttl := rr.TTL
		if ttl < minimumTTL {
			ttl = minimumTTL
		}
		if ttl > maximumTTL {
			ttl = maximumTTL
		}
		input := recordInput{Type: "TXT", Name: name, Data: rr.Data, TTL: int(ttl / time.Second)}
		var result record
		path := p.zoneURL(zone) + "/dns-records"
		if err := p.request(ctx, http.MethodPost, path, input, http.StatusCreated, &result); err != nil {
			return p.rollbackCreated(ctx, zone, created, err)
		}
		if result.ID == "" {
			return p.rollbackCreated(ctx, zone, created, errors.New("GoDaddy v3 did not return a DNS record ID"))
		}
		created = append(created, libdns.TXT{
			Name: name, Text: rr.Data, TTL: ttl, ProviderData: result.ID,
		})
	}
	return created, nil
}

// DeleteRecords removes only TXT values that exactly match the requested name and data.
func (p *Provider) DeleteRecords(ctx context.Context, zone string, records []libdns.Record) ([]libdns.Record, error) {
	zone, err := normalizeZone(zone)
	if err != nil {
		return nil, err
	}
	deleted := make([]libdns.Record, 0, len(records))
	for _, input := range records {
		rr := input.RR()
		if rr.Type != "TXT" {
			continue
		}
		name, err := relativeName(zone, rr.Name)
		if err != nil {
			return deleted, err
		}
		query := url.Values{"type": {"TXT"}, "name": {name}}
		items, err := p.listRecords(ctx, zone, query)
		if err != nil {
			return deleted, err
		}
		for _, item := range items {
			if item.Type != "TXT" || item.Name != name || item.Data != rr.Data || item.ID == "" {
				continue
			}
			path := p.zoneURL(zone) + "/dns-records/" + url.PathEscape(item.ID)
			if err := p.request(ctx, http.MethodDelete, path, nil, http.StatusNoContent, nil); err != nil {
				return deleted, err
			}
			deleted = append(deleted, libdns.TXT{Name: name, Text: item.Data, TTL: time.Duration(item.TTL) * time.Second, ProviderData: item.ID})
		}
	}
	return deleted, nil
}

func (p *Provider) listRecords(ctx context.Context, zone string, filters url.Values) ([]record, error) {
	all := make([]record, 0)
	for page := 1; ; page++ {
		query := url.Values{}
		for key, values := range filters {
			query[key] = append([]string(nil), values...)
		}
		query.Set("page", strconv.Itoa(page))
		query.Set("pageSize", strconv.Itoa(pageSize))
		var result recordsPage
		path := p.zoneURL(zone) + "/dns-records?" + query.Encode()
		if err := p.request(ctx, http.MethodGet, path, nil, http.StatusOK, &result); err != nil {
			return nil, err
		}
		all = append(all, result.Items...)
		if len(result.Items) < pageSize {
			return all, nil
		}
	}
}

func (p *Provider) request(ctx context.Context, method, path string, payload any, expected int, result any) error {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+p.APIToken)
	req.Header.Set("Accept", "application/json")
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := p.client
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	response, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("GoDaddy DNS API request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != expected {
		return fmt.Errorf("GoDaddy DNS API %s returned HTTP %d", method, response.StatusCode)
	}
	if result == nil {
		return nil
	}
	if err := json.NewDecoder(response.Body).Decode(result); err != nil {
		return fmt.Errorf("could not decode GoDaddy DNS API response: %w", err)
	}
	return nil
}

func (p *Provider) rollbackCreated(ctx context.Context, zone string, created []libdns.Record, cause error) ([]libdns.Record, error) {
	if len(created) > 0 {
		if _, err := p.DeleteRecords(ctx, zone, created); err != nil {
			return created, fmt.Errorf("%v; DNS rollback also failed: %w", cause, err)
		}
	}
	return nil, cause
}

func (p *Provider) zoneURL(zone string) string {
	return "/" + url.PathEscape(zone)
}

func normalizeZone(zone string) (string, error) {
	zone = strings.TrimSuffix(strings.TrimSpace(strings.ToLower(zone)), ".")
	if zone == "" || strings.ContainsAny(zone, "/?#") {
		return "", errors.New("invalid GoDaddy DNS zone")
	}
	return zone, nil
}

func relativeName(zone, name string) (string, error) {
	name = strings.TrimSuffix(strings.TrimSpace(strings.ToLower(name)), ".")
	if name == "" || strings.ContainsAny(name, "/?#") {
		return "", errors.New("invalid DNS record name")
	}
	if name == zone {
		return "@", nil
	}
	if strings.HasSuffix(name, "."+zone) {
		name = strings.TrimSuffix(name, "."+zone)
	}
	if name == "" {
		return "@", nil
	}
	return name, nil
}

var (
	_ caddy.Module          = (*Provider)(nil)
	_ caddy.Provisioner     = (*Provider)(nil)
	_ caddyfile.Unmarshaler = (*Provider)(nil)
	_ libdns.RecordGetter   = (*Provider)(nil)
	_ libdns.RecordAppender = (*Provider)(nil)
	_ libdns.RecordDeleter  = (*Provider)(nil)
)
