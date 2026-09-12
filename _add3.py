"""Apply batch 3 to .ilang/site.ilang.

Rule provider ids must match config.slug(name): 'AccuWeb Hosting' -> accuweb-hosting,
'A2 Hosting' -> a2-hosting. Every rule is verified live by _verify3.py before the
batch is committed.
"""
import json
from pathlib import Path

CONFIG = Path('.ilang/site.ilang')

# id, name, website, source_url, note
PROVIDERS = [
    ('shopify', 'Shopify', 'https://www.shopify.com/', 'https://www.shopify.com/pricing', 'Ecommerce platform and online store hosting.'),
    ('bigcommerce', 'BigCommerce', 'https://www.bigcommerce.com/', 'https://www.bigcommerce.com/pricing/', 'Ecommerce platform and hosting.'),
    ('duda', 'Duda', 'https://www.duda.co/', 'https://www.duda.co/pricing', 'Website builder for agencies.'),
    ('framer', 'Framer', 'https://www.framer.com/', 'https://www.framer.com/pricing/', 'Website builder and hosting.'),
    ('ghost', 'Ghost', 'https://ghost.org/', 'https://ghost.org/pricing/', 'Managed publishing platform.'),
    ('tilda', 'Tilda', 'https://tilda.cc/', 'https://tilda.cc/pricing/', 'Website builder.'),
    ('accuweb-hosting', 'AccuWeb Hosting', 'https://www.accuwebhosting.com/', 'https://www.accuwebhosting.com/pricing', 'Shared, VPS and reseller hosting.'),
    ('kamatera', 'Kamatera', 'https://www.kamatera.com/', 'https://www.kamatera.com/pricing/', 'Cloud servers.'),
    ('gridpane', 'GridPane', 'https://gridpane.com/', 'https://gridpane.com/pricing/', 'Managed WordPress hosting control panel.'),
    ('koyeb', 'Koyeb', 'https://www.koyeb.com/', 'https://www.koyeb.com/pricing', 'Application hosting platform.'),
    ('flywheel', 'Flywheel', 'https://getflywheel.com/', 'https://getflywheel.com/pricing/', 'Managed WordPress hosting.'),
    ('10web', '10Web', 'https://10web.io/', 'https://10web.io/pricing/', 'Managed WordPress and AI site builder.'),
    ('webflow', 'Webflow', 'https://webflow.com/', 'https://webflow.com/pricing', 'Website builder and hosting.'),
    ('gandi', 'Gandi', 'https://www.gandi.net/', 'https://www.gandi.net/en/domain', 'Domain registration.'),
    ('fasthosts', 'Fasthosts', 'https://www.fasthosts.co.uk/', 'https://www.fasthosts.co.uk/web-hosting', 'Shared and cloud hosting.'),
    ('name-com', 'Name.com', 'https://www.name.com/', 'https://www.name.com/domains', 'Domain registration.'),
    ('pressable', 'Pressable', 'https://pressable.com/', 'https://pressable.com/pricing/', 'Managed WordPress hosting.'),
    ('railway', 'Railway', 'https://railway.com/', 'https://railway.com/pricing', 'Application hosting platform.'),
    ('wordpress-com', 'WordPress.com', 'https://wordpress.com/', 'https://wordpress.com/pricing/', 'Managed WordPress hosting.'),
    ('heroku', 'Heroku', 'https://www.heroku.com/', 'https://www.heroku.com/pricing', 'Application hosting platform.'),
    ('krystal', 'Krystal', 'https://krystal.uk/', 'https://krystal.uk/web-hosting', 'Shared and managed hosting.'),
    ('scaleway', 'Scaleway', 'https://www.scaleway.com/', 'https://www.scaleway.com/en/pricing/', 'Cloud servers and hosting.'),
    ('site123', 'SITE123', 'https://www.site123.com/', 'https://www.site123.com/pricing', 'Website builder.'),
    ('a2-hosting', 'A2 Hosting', 'https://www.a2hosting.com/', 'https://www.a2hosting.com/web-hosting/', 'Shared and managed hosting.'),
]

# (provider, title, anchor, price pattern, currency, period, kind, condition, structure,
#  extra field patterns, mode)
RULES = [
    ('shopify', 'Basic', 'Compare all features Basic',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page states the annual-billing rate next to it.',
     'Shopify pricing card: the plan name is followed by the monthly amount.',
     None, 'anchored_text'),
    ('shopify', 'Grow', 'Grow $',
     r'Grow\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page states the annual-billing rate next to it.',
     'Shopify pricing card, read the same way as Basic.',
     None, 'anchored_text'),
    ('shopify', 'Advanced', 'Advanced $',
     r'Advanced\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page states the annual-billing rate next to it.',
     'Shopify pricing card, read the same way as Basic.',
     None, 'anchored_text'),

    ('bigcommerce', 'Core', 'Core $',
     r'Core\s*\$\s?(?P<value>[0-9,]+)/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page prints the annual-billing rate in brackets next to it.',
     'BigCommerce plan table row: the plan name is followed by the monthly rate and then the annual rate in brackets.',
     None, 'anchored_text'),
    ('bigcommerce', 'Growth', 'Growth $',
     r'Growth\s*\$\s?(?P<value>[0-9,]+)/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page prints the annual-billing rate in brackets next to it.',
     'BigCommerce plan table row, read the same way as Core.',
     None, 'anchored_text'),
    ('bigcommerce', 'Scale', 'Scale $',
     r'Scale\s*\$\s?(?P<value>[0-9,]+)/mo', 'USD', 'month', 'regular_price',
     'Monthly billing rate; the page prints the annual-billing rate in brackets next to it.',
     'BigCommerce plan table row, read the same way as Core.',
     None, 'anchored_text'),

    ('duda', 'Basic', 'Basic $',
     r'Basic\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the annual-billing rate immediately after it.',
     'Duda pricing card: the plan name is followed by the monthly rate and then the annual rate.',
     None, 'anchored_text'),
    ('duda', 'Team', 'Buy plan Team $',
     r'Team\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the annual-billing rate immediately after it.',
     'Duda pricing card, read the same way as Basic.',
     None, 'anchored_text'),
    ('duda', 'Agency', 'Best Value Agency $',
     r'Agency\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the annual-billing rate immediately after it.',
     'Duda pricing card, read the same way as Basic.',
     None, 'anchored_text'),
    ('duda', 'White Label', 'White Label $',
     r'White Label\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the annual-billing rate immediately after it.',
     'Duda pricing card, read the same way as Basic.',
     None, 'anchored_text'),

    ('framer', 'Basic', 'Basic Creative personal sites',
     r'\$\s?(?P<value>[0-9,]+) per month', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month, as printed on the official pricing page.',
     'Framer pricing card: the plan name and its one-line description are followed by the amount and "per month".',
     None, 'anchored_text'),
    ('framer', 'Pro', 'Pro Growing professional sites',
     r'\$\s?(?P<value>[0-9,]+) per month', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month, as printed on the official pricing page.',
     'Framer pricing card, read the same way as Basic.',
     None, 'anchored_text'),

    ('ghost', 'Starter', 'Starter For solo blogs & newsletters',
     r'\$\s?(?P<value>[0-9,]+) USD / mo', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month; the card states "Billed yearly".',
     'Ghost pricing card: the plan name and description are followed by the amount, the currency code and "/ mo".',
     None, 'anchored_text'),
    ('ghost', 'Publisher', 'Publisher For custom publications',
     r'\$\s?(?P<value>[0-9,]+) USD / mo', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month; the card states "Billed yearly".',
     'Ghost pricing card, read the same way as Starter.',
     None, 'anchored_text'),
    ('ghost', 'Business', 'Business For teams scaling up',
     r'\$\s?(?P<value>[0-9,]+) USD / mo', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month; the card states "Billed yearly".',
     'Ghost pricing card, read the same way as Starter.',
     None, 'anchored_text'),

    ('tilda', 'Personal', 'Personal 1 website',
     r'\$\s?(?P<value>[0-9,]+) /month with annual payment', 'USD', 'month', 'regular_price',
     'Annual-payment rate per month; the page prints the month-to-month rate further down.',
     'Tilda pricing card: the plan name and description are followed by the amount and "with annual payment".',
     None, 'anchored_text'),
    ('tilda', 'Business', 'Business 5 websites',
     r'\$\s?(?P<value>[0-9,]+) /month with annual payment', 'USD', 'month', 'regular_price',
     'Annual-payment rate per month; the page prints the month-to-month rate further down.',
     'Tilda pricing card, read the same way as Personal.',
     None, 'anchored_text'),

    ('accuweb-hosting', 'Budget', 'Cheap Hosting Budget',
     r'Budget\s*\$\s?(?P<value>[0-9.]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Rate for the printed 36-month term.',
     'AccuWeb pricing card: the plan name is followed by the monthly amount and the term it requires.',
     None, 'anchored_text'),
    ('accuweb-hosting', 'Bootstrap ++', 'Bootstrap ++',
     r'Bootstrap \+{2}\s*\$\s?(?P<value>[0-9.]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Rate for the printed three-year term.',
     'AccuWeb pricing card, read the same way as Budget.',
     None, 'anchored_text'),
    ('accuweb-hosting', 'Premium ++', 'Premium ++',
     r'Premium \+{2}\s*\$\s?(?P<value>[0-9.]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Rate for the printed three-year term.',
     'AccuWeb pricing card, read the same way as Budget.',
     None, 'anchored_text'),

    ('kamatera', 'Basic', 'Forex Basic',
     r'Basic\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Starting monthly price of the printed server size.',
     'Kamatera server card: the plan name is followed by the monthly amount and the server specification.',
     None, 'anchored_text'),
    ('kamatera', 'Standard', 'Dallas Standard',
     r'Standard\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Starting monthly price of the printed server size.',
     'Kamatera server card, read the same way as Basic.',
     None, 'anchored_text'),
    ('kamatera', 'Pro', 'Dallas Pro',
     r'Pro\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Starting monthly price of the printed server size.',
     'Kamatera server card, read the same way as Basic.',
     None, 'anchored_text'),

    ('gridpane', 'PeakFreq', 'PeakFreq From as low as:',
     r'\$\s?(?P<value>[0-9,]+)\s*/month', 'USD', 'month', 'regular_price',
     'Starting price the page prints for this tier.',
     'GridPane pricing card: the tier name is followed by "From as low as:" and the monthly amount.',
     None, 'anchored_text'),
    ('gridpane', 'Bespoke Hosting', 'Bespoke Hosting From as low as:',
     r'\$\s?(?P<value>[0-9,]+)\s*/year', 'USD', 'year', 'regular_price',
     'Starting price the page prints for this tier.',
     'GridPane pricing card, read the same way as PeakFreq.',
     None, 'anchored_text'),

    ('koyeb', 'Pro', 'Add support plans and capacity. Pro',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Platform fee per month; compute is charged on top, as the page states with "+compute".',
     'Koyeb pricing card: the plan name is followed by the monthly platform fee and the "+compute" qualifier.',
     None, 'anchored_text'),
    ('koyeb', 'Scale', 'Get started Scale',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Platform fee per month; compute is charged on top, as the page states with "+compute".',
     'Koyeb pricing card, read the same way as Pro.',
     None, 'anchored_text'),

    ('flywheel', 'Starter', 'Starter $',
     r'Starter\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     'Flywheel pricing card: the plan name is followed by the monthly amount; the yearly total is printed separately.',
     None, 'anchored_text'),
    ('flywheel', 'Freelance', 'Choose Plan Freelance $',
     r'Freelance\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     'Flywheel pricing card, read the same way as Starter.',
     None, 'anchored_text'),
    ('flywheel', 'Agency', 'Choose Plan Agency $',
     r'Agency\s*\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     'Flywheel pricing card, read the same way as Starter.',
     None, 'anchored_text'),

    ('10web', 'AI Starter', 'AI Starter',
     r'AI Starter\s*\$\s?(?P<value>[0-9.,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     '10Web pricing card: the plan name is followed by the monthly amount.',
     None, 'anchored_text'),
    ('10web', 'AI Premium', 'AI Premium',
     r'AI Premium\s*\$\s?(?P<value>[0-9.,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     '10Web pricing card, read the same way as AI Starter.',
     None, 'anchored_text'),
    ('10web', 'AI Ultimate', 'AI Ultimate',
     r'AI Ultimate\s*\$\s?(?P<value>[0-9.,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     '10Web pricing card, read the same way as AI Starter.',
     None, 'anchored_text'),
    ('10web', 'Agency starter', 'Agency starter',
     r'Agency starter\s*\$\s?(?P<value>[0-9.,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     '10Web pricing card, read the same way as AI Starter.',
     None, 'anchored_text'),
    ('10web', 'Agency core', 'Agency core',
     r'Agency core\s*\$\s?(?P<value>[0-9.,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Monthly rate the page prints for the plan.',
     '10Web pricing card, read the same way as AI Starter.',
     None, 'anchored_text'),

    ('webflow', 'Core', 'Core For more staging needs.',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month, as printed next to "billed yearly".',
     'Webflow platform plan card: the plan name and description are followed by the amount and "/mo billed yearly".',
     None, 'anchored_text'),
    ('webflow', 'Plus', 'Plus Best for higher volume businesses.',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Yearly-billing rate per month, as printed next to "billed yearly".',
     'Webflow platform plan card, read the same way as Core.',
     None, 'anchored_text'),
    ('webflow', 'Optimize', 'Optimize Maximize conversions from your site.',
     r'\$\s?(?P<value>[0-9,]+)\s*/mo', 'USD', 'month', 'regular_price',
     'Starting rate per month, as printed before the usage note.',
     'Webflow platform plan card, read the same way as Core.',
     None, 'anchored_text'),

    ('gandi', '.com registration', 'Domain names selected for you: TLDs Register Renew Transfer .com',
     r'\.com\s*€(?P<value>[0-9.]+)\s*€(?P<renewal>[0-9.]+)', 'EUR', 'year', 'regular_price',
     'Registration price; the second amount in the row is the renewal price.',
     'Gandi domain table row: the extension is followed by the registration, renewal and transfer amounts in that order.',
     {'renewal_price': r'\.com\s*€[0-9.]+\s*€(?P<value>[0-9.]+)'}, 'anchored_text'),
    ('gandi', '.fr registration', 'Domain names selected for you: TLDs Register Renew Transfer .com',
     r'\.fr\s*€(?P<value>[0-9.]+)\s*€(?P<renewal>[0-9.]+)', 'EUR', 'year', 'regular_price',
     'Registration price; the second amount in the row is the renewal price.',
     'Gandi domain table row, read the same way as .com.',
     {'renewal_price': r'\.fr\s*€[0-9.]+\s*€(?P<value>[0-9.]+)'}, 'anchored_text'),

    ('fasthosts', 'Start', 'Start Starts at',
     r'Starts at £(?P<value>[0-9.]+)', 'GBP', 'month', 'promotion',
     'Introductory monthly rate for the printed period; the page states the rate charged afterwards.',
     'Fasthosts comparison row: the plan name is followed by the introductory amount and then the rate charged afterwards.',
     {'renewal_price': r'then £(?P<value>[0-9.]+)'}, 'anchored_text'),
    ('fasthosts', 'Scale', 'Scale Starts at',
     r'Starts at £(?P<value>[0-9.]+)', 'GBP', 'month', 'promotion',
     'Introductory monthly rate for the printed period; the page states the rate charged afterwards.',
     'Fasthosts comparison row, read the same way as Start.',
     {'renewal_price': r'then £(?P<value>[0-9.]+)'}, 'anchored_text'),
    ('fasthosts', 'Pro', 'Pro Starts at',
     r'Starts at £(?P<value>[0-9.]+)', 'GBP', 'month', 'promotion',
     'Introductory monthly rate for the printed period; the page states the rate charged afterwards.',
     'Fasthosts comparison row, read the same way as Start.',
     {'renewal_price': r'then £(?P<value>[0-9.]+)'}, 'anchored_text'),

    ('name-com', '.com registration', 'All Aftermarket Domains Featured Domains',
     r'\.com\s*\$\s?(?P<value>[0-9.]+)\s*\$\s?(?P<renewal>[0-9.]+)', 'USD', 'year', 'regular_price',
     'Registration price; the second amount in the row is the renewal price.',
     'Name.com featured-domain row: the extension is followed by the registration amount and then the renewal amount.',
     {'renewal_price': r'\.com\s*\$\s?[0-9.]+\s*\$\s?(?P<value>[0-9.]+)'}, 'anchored_text'),
    ('name-com', '.net registration', 'All Aftermarket Domains Featured Domains',
     r'\.net\s*\$\s?(?P<value>[0-9.]+)\s*\$\s?(?P<renewal>[0-9.]+)', 'USD', 'year', 'regular_price',
     'Registration price; the second amount in the row is the renewal price.',
     'Name.com featured-domain row, read the same way as .com.',
     {'renewal_price': r'\.net\s*\$\s?[0-9.]+\s*\$\s?(?P<value>[0-9.]+)'}, 'anchored_text'),
    ('name-com', '.org registration', 'All Aftermarket Domains Featured Domains',
     r'\.org\s*\$\s?(?P<value>[0-9.]+)\s*\$\s?(?P<renewal>[0-9.]+)', 'USD', 'year', 'regular_price',
     'Registration price; the second amount in the row is the renewal price.',
     'Name.com featured-domain row, read the same way as .com.',
     {'renewal_price': r'\.org\s*\$\s?[0-9.]+\s*\$\s?(?P<value>[0-9.]+)'}, 'anchored_text'),

    ('pressable', 'Signature 1', 'Signature 1',
     r'Billed \$\s?(?P<value>[0-9,]+) USD per year', 'USD', 'year', 'regular_price',
     'Yearly total the page prints; the monthly equivalent is shown separately.',
     'Pressable pricing card: the plan name is followed by the monthly rate and then the yearly total billed.',
     None, 'anchored_text'),
    ('pressable', 'Signature 3', 'Signature 3',
     r'Billed \$\s?(?P<value>[0-9,]+) USD per year', 'USD', 'year', 'regular_price',
     'Yearly total the page prints; the monthly equivalent is shown separately.',
     'Pressable pricing card, read the same way as Signature 1.',
     None, 'anchored_text'),
    ('pressable', 'Signature 5', 'Signature 5',
     r'Billed \$\s?(?P<value>[0-9,]+) USD per year', 'USD', 'year', 'regular_price',
     'Yearly total the page prints; the monthly equivalent is shown separately.',
     'Pressable pricing card, read the same way as Signature 1.',
     None, 'anchored_text'),
    ('pressable', 'Signature 8', 'Signature 8',
     r'Billed \$\s?(?P<value>[0-9,]+) USD per year', 'USD', 'year', 'regular_price',
     'Yearly total the page prints; the monthly equivalent is shown separately.',
     'Pressable pricing card, read the same way as Signature 1.',
     None, 'anchored_text'),

    ('railway', 'Hobby', 'Hobby $',
     r'Hobby\s*\$\s?(?P<value>[0-9,]+) minimum usage', 'USD', 'month', 'regular_price',
     'Minimum monthly usage the plan requires; usage above it is billed as it is used.',
     'Railway pricing card: the plan name is followed by the minimum monthly usage amount.',
     None, 'anchored_text'),
    ('railway', 'Pro', 'Pro $',
     r'Pro\s*\$\s?(?P<value>[0-9,]+) minimum usage', 'USD', 'month', 'regular_price',
     'Minimum monthly usage the plan requires; usage above it is billed as it is used.',
     'Railway pricing card, read the same way as Hobby.',
     None, 'anchored_text'),

    ('wordpress-com', 'Personal', 'Personal Build your presence with a site you can customize.',
     r'\$\s?(?P<value>[0-9.]+)', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the yearly, two-year and three-year rates after it.',
     'WordPress.com pricing card: the plan name and description are followed by the monthly rate and then the longer-term rates.',
     None, 'anchored_text'),
    ('wordpress-com', 'Premium', 'Premium Accept payments on your site and reach more people.',
     r'\$\s?(?P<value>[0-9.]+)', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the yearly, two-year and three-year rates after it.',
     'WordPress.com pricing card, read the same way as Personal.',
     None, 'anchored_text'),
    ('wordpress-com', 'Business', 'Business Grow your business with powerful tools and priority support.',
     r'\$\s?(?P<value>[0-9.]+)', 'USD', 'month', 'regular_price',
     'Monthly-billing rate; the page prints the yearly, two-year and three-year rates after it.',
     'WordPress.com pricing card, read the same way as Personal.',
     None, 'anchored_text'),

    ('heroku', 'Eco', 'Eco $',
     r'Eco\s*\$\s?(?P<value>[0-9,]+)\s*0\.5 GB', 'USD', 'month', 'regular_price',
     'Monthly price of the dyno type as printed in the official table.',
     'Heroku dyno table row: the dyno type is followed by the monthly price and then its RAM and compute share.',
     None, 'anchored_text'),
    ('heroku', 'Basic', 'Basic $',
     r'Basic\s*\$\s?(?P<value>[0-9,]+)\s*0\.5 GB', 'USD', 'month', 'regular_price',
     'Monthly price of the dyno type as printed in the official table.',
     'Heroku dyno table row, read the same way as Eco.',
     None, 'anchored_text'),
    ('heroku', 'Standard-1X', 'Standard-1X $',
     r'Standard-1X\s*\$\s?(?P<value>[0-9,]+)\s*0\.5 GB', 'USD', 'month', 'regular_price',
     'Monthly price of the dyno type as printed in the official table.',
     'Heroku dyno table row, read the same way as Eco.',
     None, 'anchored_text'),
    ('heroku', 'Standard-2X', 'Standard-2X $',
     r'Standard-2X\s*\$\s?(?P<value>[0-9,]+)\s*1 GB', 'USD', 'month', 'regular_price',
     'Monthly price of the dyno type as printed in the official table.',
     'Heroku dyno table row, read the same way as Eco.',
     None, 'anchored_text'),

    ('krystal', 'Amethyst', 'Amethyst £',
     r'Amethyst\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card: the plan name is followed by the monthly amount and "/ month".',
     None, 'anchored_text'),
    ('krystal', 'Ruby', 'Ruby £',
     r'Ruby\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card, read the same way as Amethyst.',
     None, 'anchored_text'),
    ('krystal', 'Emerald', 'Emerald £',
     r'Emerald\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card, read the same way as Amethyst.',
     None, 'anchored_text'),
    ('krystal', 'Sapphire', 'Sapphire £',
     r'Sapphire\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card, read the same way as Amethyst.',
     None, 'anchored_text'),
    ('krystal', 'Diamond', 'Diamond £',
     r'Diamond\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card, read the same way as Amethyst.',
     None, 'anchored_text'),
    ('krystal', 'Tanzanite', 'Tanzanite £',
     r'Tanzanite\s*£(?P<value>[0-9.]+)\s*/ month', 'GBP', 'month', 'regular_price',
     'Monthly rate the page prints; the page states whether VAT is included.',
     'Krystal pricing card, read the same way as Amethyst.',
     None, 'anchored_text'),

    ('scaleway', 'Start', 'Start Affordable servers with the best price-performance ratio on the market starting at',
     r'€(?P<value>[0-9.]+)/month', 'EUR', 'month', 'regular_price',
     'Starting price the page prints for this server family.',
     'Scaleway product card: the family name and its one-line description are followed by the starting monthly price.',
     None, 'anchored_text'),

    ('site123', 'Custom domain plan', 'you will be charged as low as',
     r'\$\s?(?P<value>[0-9.]+) per month', 'USD', 'month', 'regular_price',
     'Annual-plan rate per month, as the page states next to the amount.',
     'SITE123 pricing note: the amount is printed in the sentence about the annual plan.',
     None, 'anchored_text'),

    ('a2-hosting', '.com registration', 'get dot com domain',
     r'font-bold">\$\s?(?P<value>[0-9.]+)', 'USD', 'year', 'regular_price',
     'First-year registration price, as printed under the domain offer.',
     'A2 Hosting domain offer: the domain image alt text is followed by the first-year amount.',
     None, 'anchored_html'),
    ('a2-hosting', '.org registration', 'buy dot org domains',
     r'font-bold">\$\s?(?P<value>[0-9.]+)', 'USD', 'year', 'regular_price',
     'First-year registration price, as printed under the domain offer.',
     'A2 Hosting domain offer, read the same way as .com.',
     None, 'anchored_html'),
    ('a2-hosting', '.online registration', 'buy dot online domains',
     r'font-bold">\$\s?(?P<value>[0-9.]+)', 'USD', 'year', 'regular_price',
     'First-year registration price, as printed under the domain offer.',
     'A2 Hosting domain offer, read the same way as .com.',
     None, 'anchored_html'),
]

EXCLUDED = [
    'Rocket.net | robots.txt returned HTTP 403 at https://rocket.net/robots.txt (checked 2026-09-12), so crawl permission could not be established and the pricing page was not requested.',
    'Surge | robots.txt returned HTTP 404 at https://surge.sh/robots.txt (checked 2026-09-12); no crawl permission could be established.',
    'Domain.com | robots.txt returned HTTP 403 at https://www.domain.com/robots.txt (checked 2026-09-12), so crawl permission could not be established and the domain page was not requested.',
    'HostPapa | https://www.hostpapa.com/web-hosting/ returned HTTP 404 (checked 2026-09-12); the web-hosting path does not exist. https://www.hostpapa.com/hosting/ also returned HTTP 404.',
    'Krystal (krystal.io) | https://krystal.io/pricing returned HTTP 404 (checked 2026-09-12); the current site is served from krystal.uk, which was landed instead.',
]


def build_rule(row):
    (pid, title, anchor, price, currency, period, kind, condition, structure) = row[:9]
    extra = row[9] if len(row) > 9 else None
    mode = row[10] if len(row) > 10 else 'anchored_text'
    rule = {
        'provider': pid, 'mode': mode, 'title': title,
        'category': 'Hosting', 'kind': kind, 'anchor': anchor,
        'window_chars': 6000, 'currency': currency, 'billing_period': period,
        'required_fields': ['price'],
        'field_patterns': {'price': price},
        'condition': condition, 'structure': structure,
    }
    if extra:
        rule['field_patterns'].update(extra)
    return rule


def add_providers(text, rows):
    lines = text.split('\n')
    for index, line in enumerate(lines):
        if line.startswith('::MODULE{PROVIDERS'):
            body = [f'{name} | {website} | {source} |' for _pid, name, website, source, _note in rows]
            lines[index + 1:index + 1] = body
            return '\n'.join(lines), len(body)
    raise SystemExit('PROVIDERS module not found')


def add_notes(text, rows):
    lines = text.split('\n')
    for index, line in enumerate(lines):
        if '"hostinger":"VPS and web hosting."' in line:
            row = json.loads(line.strip())
            for pid, _name, _website, _source, note in rows:
                row[pid] = note
            lines[index] = json.dumps(row, ensure_ascii=False, separators=(',', ':'))
            return '\n'.join(lines), len(row)
    raise SystemExit('notes row not found')


def add_rules(text, rows):
    lines = text.split('\n')
    body = [json.dumps(build_rule(row), ensure_ascii=False, separators=(',', ':')) for row in rows]
    for index, line in enumerate(lines):
        if line.startswith('::MODULE{EXTRACTORS'):
            lines[index + 1:index + 1] = body
            return '\n'.join(lines), len(body)
    raise SystemExit('EXTRACTORS module not found')


def add_excluded(text, rows):
    lines = text.split('\n')
    for index, line in enumerate(lines):
        if line.startswith('::MODULE{EXCLUDED'):
            lines[index + 1:index + 1] = list(rows)
            return '\n'.join(lines), len(rows)
    raise SystemExit('EXCLUDED module not found')


if __name__ == '__main__':
    text = CONFIG.read_text(encoding='utf-8')
    text, providers = add_providers(text, PROVIDERS)
    text, notes = add_notes(text, PROVIDERS)
    text, rules = add_rules(text, RULES)
    text, excluded = add_excluded(text, EXCLUDED)
    CONFIG.write_text(text, encoding='utf-8')
    print(f'providers added {providers}, notes now {notes}, rules added {rules}, excluded added {excluded}')
