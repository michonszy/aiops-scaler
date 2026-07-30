import http from 'k6/http';
import { check, sleep } from 'k6';
export const options = {
  scenarios: {
    hpa_attack_pattern: {
      executor: 'ramping-vus',
      startVUs: 0,
    stages: [
      // Let HPA reach minimum replicas
      { duration: '5m', target: 50 },
      // Sudden burst
      { duration: '30s', target: 1500 },
      // Give HPA time to react, but observe damage
      { duration: '5m', target: 1500 },
      // Traffic disappears
      // Keep it low longer than stabilization window
      { duration: '5m', target: 100 },
      // Second burst after HPA finally scaled down
      { duration: '30s', target: 1000 },
      // Observe recovery
      { duration: '4m', target: 1000 },
      // // Final cooldown
      // { duration: '5m', target: 5 },
    ]
    },
  },
  thresholds: {
    // Show degradation
    http_req_duration: [
      'p(95)<5000',
      'p(99)<10000'
    ],
    // Track availability
    http_req_failed: [
      'rate<0.05'
    ],
  },
};
const BASE_URL =
  'http://35.239.253.186';
function randomProduct() {
  const products = [
    '0PUK6V6EV0', // Corrected (Sunglasses)
    '1YMWWN1N4O', // (Home Barista Kit)
    'L9ECAV7KIM', // (Terrarium)
    '66VCHSJNUP', // (Vintage Camera Lens)
    '2ZYFJ3GM2N'  // Corrected (Film Camera)
  ];
  return products[
    Math.floor(Math.random()*products.length)
  ];
}
export default function () {
  // Homepage
  let res =
    http.get(`${BASE_URL}/`);
  check(res,{
    'homepage': r=>r.status===200
  });
  // Product browsing
  let product=randomProduct();
  res=http.get(
    `${BASE_URL}/product/${product}`
  );
  check(res,{
    'product page':
      r=>r.status===200
  });
  // Add to cart
  res=http.post(
    `${BASE_URL}/cart`,
    {
      product_id: product,
      quantity:
        Math.floor(Math.random()*5)+1
    }
  );
  check(res,{
    'cart add':
      r=>r.status===200 ||
         r.status===302
  });
  // Currency switching
  const currencies=[
    'USD',
    'EUR',
    'JPY',
    'GBP'
  ];
  res=http.post(
    `${BASE_URL}/setCurrency`,
    {
      currency_code:
        currencies[
          Math.floor(
            Math.random()*currencies.length
          )
        ]
    }
  );
  // Checkout pressure
  res=http.post(
    `${BASE_URL}/cart/checkout`,
    {
      email:
        `load-${__VU}@example.com`,
      street_address:
        "123 Chaos Street",
      zip_code:
        "12345",
      city:
        "Load City",
      state:
        "CA",
      country:
        "US",
      credit_card_number:
        "4111111111111111",
      credit_card_expiration_month:
        "12",
      credit_card_expiration_year:
        "2030",
      credit_card_cvv:
        "123"
    }
  );
  check(res,{
    'checkout':
      r=>r.status===200 ||
         r.status===302
  });
  // small think time
  sleep(
    Math.random()*2
  );
}