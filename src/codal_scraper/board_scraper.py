"""
Async board members scraper for extracting detailed board information from Codal pages

This module provides a Playwright-based scraper for extracting board member
information from Codal announcement pages.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd
import networkx as nx

from .utils import (
    clean_dict, normalize_persian_text, persian_to_english_digits,
    datetime_to_num, year_month_from_date, is_independent_duty
)
from .constants import BOARD_MEMBER_SELECTORS
from .types import BoardMemberData, ScrapingErrorInfo, ScrapingErrorType
from .exceptions import ScrapingError


logger = logging.getLogger(__name__)


class BoardMemberScraper:
    """
    Async scraper for extracting board member information from Codal URLs.
    
    This scraper uses Playwright to navigate and extract detailed board member
    information including names, national IDs, education, and positions.
    
    Features:
        - Async/await support for concurrent scraping
        - Network graph generation for member relationships
        - Automatic retry for failed pages
        - Detailed error tracking
        - Export to Excel/CSV with summaries
    
    Example:
        >>> async with BoardMemberScraper() as scraper:
        ...     df = await scraper.scrape_urls(urls)
        ...     scraper.export_to_excel(df, "board_members.xlsx")
        ...     scraper.visualize_network("network.html")
    
    Attributes:
        members_data: List of extracted board member records
        network: NetworkX graph of company-member relationships
        errors: List of scraping errors encountered
    """
    
    def __init__(
        self,
        selectors: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
        timeout: int = 60,
        headless: bool = True,
        max_concurrent: int = 5
    ):
        """
        Initialize the board member scraper.
        
        Args:
            selectors: Custom CSS selectors for extracting information.
                      Defaults to standard Codal page selectors.
            max_retries: Maximum retry attempts for failed pages.
            timeout: Page load timeout in seconds.
            headless: Run browser in headless mode.
            max_concurrent: Maximum concurrent page loads.
        """
        self.members_data: List[BoardMemberData] = []
        self.network = nx.Graph()
        self.errors: List[ScrapingErrorInfo] = []
        self.max_retries = max_retries
        self.timeout = timeout
        self.headless = headless
        self.max_concurrent = max_concurrent
        
        # CSS selectors for extracting data
        self.selectors = selectors or BOARD_MEMBER_SELECTORS.copy()
        
        # Crawler instance (lazy initialization)
        self._crawler = None
        self._is_initialized = False
        
        # Statistics
        self._stats = {
            'urls_processed': 0,
            'urls_succeeded': 0,
            'urls_failed': 0,
            'members_extracted': 0,
            'start_time': None,
            'end_time': None
        }
    
    async def __aenter__(self) -> 'BoardMemberScraper':
        """Async context manager entry"""
        await self.initialize_crawler()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Async context manager exit - cleanup resources"""
        await self.close()
        return False
    
    async def initialize_crawler(self) -> None:
        """Initialize the Playwright crawler"""
        if self._is_initialized:
            return
        
        try:
            from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
            
            self._crawler = PlaywrightCrawler(
                headless=self.headless,
                browser_type='chromium',
                max_requests_per_crawl=1000,
                request_handler_timeout=timedelta(seconds=self.timeout),
            )
            
            # Register the handler
            @self._crawler.router.default_handler
            async def handler(context: PlaywrightCrawlingContext) -> None:
                await self._request_handler(context)
            
            self._is_initialized = True
            logger.info("Playwright crawler initialized")
            
        except ImportError:
            raise ImportError(
                "crawlee with playwright is required for board scraping. "
                "Install with: pip install 'crawlee[playwright]'"
            )
    
    async def close(self) -> None:
        """Properly close crawler and release resources"""
        if self._crawler:
            try:
                logger.info("Closing crawler...")
            except Exception as e:
                logger.warning(f"Error during crawler cleanup: {e}")
            finally:
                self._crawler = None
                self._is_initialized = False
        
        self._stats['end_time'] = datetime.now().isoformat()
        logger.info("Scraper resources released")
    
    async def _request_handler(self, context) -> None:
        """
        Handle each URL request.
        
        Args:
            context: Playwright crawling context
        """
        from crawlee.crawlers import PlaywrightCrawlingContext
        
        url = context.request.url
        self._stats['urls_processed'] += 1
        logger.info(f"Processing [{self._stats['urls_processed']}]: {url}")
        
        try:
            # Navigate to the page
            await context.page.goto(
                url,
                timeout=self.timeout * 1000,
                wait_until='domcontentloaded'
            )
            
            # Wait for dynamic content
            await context.page.wait_for_timeout(2000)
            
            # Check if table exists
            table_exists = await context.page.query_selector(
                self.selectors['table']
            )
            
            if table_exists:
                await self._scrape_board_members(context)
                self._stats['urls_succeeded'] += 1
            else:
                self._record_error(
                    url,
                    ScrapingErrorType.SELECTOR_NOT_FOUND,
                    "Board member table not found"
                )
                self._stats['urls_failed'] += 1
                
        except asyncio.TimeoutError:
            self._record_error(
                url,
                ScrapingErrorType.TIMEOUT,
                f"Page load timeout after {self.timeout}s"
            )
            self._stats['urls_failed'] += 1
            
        except Exception as e:
            error_msg = str(e).lower()
            
            if 'net::' in error_msg or 'network' in error_msg:
                error_type = ScrapingErrorType.NETWORK
            elif 'navigation' in error_msg:
                error_type = ScrapingErrorType.NAVIGATION
            elif 'timeout' in error_msg:
                error_type = ScrapingErrorType.TIMEOUT
            else:
                error_type = ScrapingErrorType.UNKNOWN
            
            self._record_error(url, error_type, str(e))
            self._stats['urls_failed'] += 1
            logger.exception(f"Error processing {url}")
    
    async def _scrape_board_members(self, context) -> None:
        """
        Scrape board member information from the page.
        
        Args:
            context: Playwright crawling context
        """
        try:
            page = context.page
            url = context.request.url
            
            # Extract all rows
                        # Extract all rows
            rows = await page.query_selector_all(self.selectors['table'])

            # Fallback: if no data rows matched, try the broader selector
            if not rows:
                logger.debug(
                    "No board-member rows found with primary selector; "
                    "falling back to 'table_all'"
                )
                rows = await page.query_selector_all(self.selectors['table_all'])

            # Extract metadata
            company_symbol = await self._extract_text(page, self.selectors['company'])
            ceo_name = await self._extract_text(page, self.selectors['ceo_name'])
            ceo_national_id = await self._extract_text(page, self.selectors['ceo_national_id'])
            ceo_degree = await self._extract_text(page, self.selectors['ceo_degree'])
            ceo_major = await self._extract_text(page, self.selectors['ceo_major'])
            
            # Extract dates and navigation
            date = await self._extract_text(page, self.selectors['date'])
            assembly_date = await self._extract_text(page, self.selectors['assembly_date'])
            has_prev = await self._element_exists(page, self.selectors['prev_version'])
            has_next = await self._element_exists(page, self.selectors['next_version'])
            
            # Process date
            date_num = ""
            month = ""
            year = ""
            if date:
                date_value = datetime_to_num(date)
                if date_value is not None:
                    date_num = str(date_value)[:8]
                    year, month = year_month_from_date(date)
                else:
                    logger.warning(
                        f"Unparseable session date {date!r} at {url}; "
                        "leaving date/year/month empty"
                    )
            
            # Normalize company symbol
            company_symbol = normalize_persian_text(company_symbol) if company_symbol else "Unknown"
            
            # Process assembly date
            if assembly_date:
                assembly_date = persian_to_english_digits(assembly_date).replace('/', '')
            
            # Process each board member row
            for row in rows:
                try:
                    member_info = await row.inner_text()
                    
                    if not member_info or not member_info.strip():
                        continue
                    
                    info = self._parse_member_row(member_info)
                    
                    if not info or len(info) < 12:
                        logger.debug(f"Skipping incomplete row with {len(info) if info else 0} fields")
                        continue
                    
                    # Create member data
                                    # Column index mapping for board member grid
                    num_cols = len(info)

                    idx_prev_member = 0
                    idx_new_member = 1
                    idx_member_id = 2
                    idx_prev_rep = 3
                    idx_new_rep = 4
                    idx_national_id = 5
                    idx_position = 6
                    idx_duty = 7
                    idx_degree = 8
                    idx_major = 9

                # Newer layout: experience + 4 yes/no columns + verification status (total 16)
                    if num_cols >= 16:
                        idx_experience = 10
                        idx_multi_exec = 11
                        idx_multi_nonexec = 12
                        idx_declaration = 13
                        idx_acceptance = 14
                        idx_verification = 15
                    elif num_cols >= 12:
                        # Older layout without explicit experience / declaration / acceptance / verification
                        idx_experience = -1
                        idx_multi_exec = 10
                        idx_multi_nonexec = 11
                        idx_declaration = -1
                        idx_acceptance = -1
                        idx_verification = -1
                    else:
                        # Safety guard – shouldn't trigger due to earlier check
                        logger.debug(
                            f"Unexpected column count ({num_cols}) for row at {url}, skipping."
                        )
                        continue

                    duty_text = self._get_text_value(info, idx_duty)
                    experience = (
                        self._get_text_value(info, idx_experience)
                        if idx_experience >= 0 else ""
                    )

                    has_multiple_executive = (
                        self._parse_yes_no_value(self._get_cell_value(info, idx_multi_exec))
                        if idx_multi_exec >= 0 else False
                    )
                    has_multiple_non_executive = (
                        self._parse_yes_no_value(self._get_cell_value(info, idx_multi_nonexec))
                        if idx_multi_nonexec >= 0 else False
                    )
                    has_corporate_declaration = (
                        self._parse_yes_no_value(self._get_cell_value(info, idx_declaration))
                        if idx_declaration >= 0 else False
                    )
                    has_position_acceptance = (
                        self._parse_yes_no_value(self._get_cell_value(info, idx_acceptance))
                        if idx_acceptance >= 0 else False
                    )
                    verification_status = (
                        self._get_text_value(info, idx_verification)
                        if idx_verification >= 0 else ""
                    )

                # Create member data
                    member_data: BoardMemberData = {
                        'year': year,
                        'date': date_num,
                        'month': month,
                        'assembly_date': assembly_date or "",
                        'company': company_symbol,
                        'has_previous': has_prev,
                        'has_next': has_next,
                        'url': url,

                        'prev_member': self._get_text_value(info, idx_prev_member),
                        'new_member': self._get_text_value(info, idx_new_member),
                        'member_id': persian_to_english_digits(
                            self._get_cell_value(info, idx_member_id)
                        ),
                        'prev_representative': self._get_text_value(info, idx_prev_rep),
                        'new_representative': self._get_text_value(info, idx_new_rep),
                        'national_id': persian_to_english_digits(
                            self._get_cell_value(info, idx_national_id)
                        ),
                        'position': self._get_text_value(info, idx_position),

                        # "غیر موظف" -> non-executive / independent
                        'is_independent': is_independent_duty(duty_text),

                        'degree': self._get_text_value(info, idx_degree),
                        'major': self._get_text_value(info, idx_major),
                        'experience': experience,

                        'has_multiple_executive': has_multiple_executive,
                        'has_multiple_non_executive': has_multiple_non_executive,
                        'has_corporate_declaration': has_corporate_declaration,
                        'has_position_acceptance': has_position_acceptance,
                        'verification_status': verification_status,

                        'ceo_name': normalize_persian_text(ceo_name) if ceo_name else "",
                        'ceo_national_id': persian_to_english_digits(
                            ceo_national_id.strip()
                        ) if ceo_national_id else "",
                        'ceo_degree': normalize_persian_text(ceo_degree) if ceo_degree else "",
                        'ceo_major': normalize_persian_text(ceo_major) if ceo_major else "",
                        'scrape_timestamp': datetime.now().isoformat()
                    }
                    
                    self.members_data.append(member_data)
                    self._stats['members_extracted'] += 1
                    
                    # Update network graph
                    if member_data['new_member']:
                        self._update_network(member_data)
                    
                except Exception as e:
                    logger.error(f"Failed to extract row at {url}: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to scrape board members at {context.request.url}: {e}")
            raise
    
    async def _extract_text(self, page, selector: str) -> Optional[str]:
        """Extract text from an element"""
        try:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
                return text.strip() if text else None
            return None
        except Exception as e:
            logger.warning(f"Failed to extract text for {selector}: {e}")
            return None
    
    async def _element_exists(self, page, selector: str) -> bool:
        """Check if an element exists on the page"""
        try:
            element = await page.query_selector(selector)
            return element is not None
        except Exception:
            return False
        
    def _parse_member_row(self, member_info: str) -> Optional[List[str]]:
        """Parse a raw row text into a list of cell values.

        Codal's ASP.NET grid renders each <td> as a tab-separated cell in
        Playwright's inner_text(). We trim whitespace and keep empty cells
        (except trailing layout-only empties) so column indices remain stable
        across older and newer layouts.
        """
        try:
            # Split on tabs and strip whitespace from each part
            parts = [part.strip() for part in member_info.split('\t')]

            # Drop trailing completely empty cells (layout artefacts)
            while parts and not parts[-1]:
                parts.pop()

            return parts if parts else None
        except Exception as e:
            logger.error(f"Error parsing member info: {e}")
            return None

    def _get_cell_value(self, cells: List[str], index: int) -> str:
        """Safely get a raw cell value by index as a stripped string."""
        if index < 0 or index >= len(cells):
            return ""
        value = cells[index]
        return value.strip() if isinstance(value, str) else ""

    def _get_text_value(self, cells: List[str], index: int) -> str:
        """Get normalized Persian text for a cell (or empty string)."""
        raw = self._get_cell_value(cells, index)
        return normalize_persian_text(raw) if raw else ""

    def _parse_yes_no_value(self, value: str) -> bool:
        """Convert Codal yes/no cells (بله/خیر) to boolean.

        We normalize the text and support a small set of English fallbacks.
        """
        if not value:
            return False

        normalized = normalize_persian_text(value)
        if not normalized:
            return False

        lowered = normalized.lower()
        return normalized == "بله" or lowered in ("yes", "true", "1")

    def _update_network(self, member_data: BoardMemberData) -> None:
        """Update the network graph with member connections"""
        try:
            company = member_data['company']
            member = member_data['new_member']
            
            if not company or not member:
                return
            
            # Add company node
            if company not in self.network:
                self.network.add_node(company, node_type='company')
            
            # Add member node
            if member not in self.network:
                self.network.add_node(
                    member,
                    node_type='person',
                    degree=member_data.get('degree', ''),
                    major=member_data.get('major', '')
                )
            
            # Add edge
            self.network.add_edge(
                company,
                member,
                position=member_data.get('position', ''),
                year=member_data.get('year', ''),
                is_independent=member_data.get('is_independent', False)
            )
            
        except Exception as e:
            logger.error(f"Error updating network: {e}")
    
    def _record_error(
        self,
        url: str,
        error_type: ScrapingErrorType,
        message: str,
        recoverable: bool = True
    ) -> None:
        """Record an error with full context"""
        error = ScrapingErrorInfo(
            url=url,
            error_type=error_type,
            message=message,
            recoverable=recoverable
        )
        self.errors.append(error)
        logger.error(f"[{error_type.value}] {url}: {message}")
    
    async def scrape_urls(
        self,
        urls: Union[List[str], pd.DataFrame],
        url_column: str = 'url'
    ) -> pd.DataFrame:
        """
        Scrape board member information from a list of URLs.
        
        Args:
            urls: List of URLs or DataFrame with URL column
            url_column: Name of URL column if DataFrame is provided
            
        Returns:
            DataFrame with scraped board member information
        """
        self._stats['start_time'] = datetime.now().isoformat()
        
        # Convert DataFrame to list if needed
        if isinstance(urls, pd.DataFrame):
            if url_column in urls.columns:
                urls = urls[url_column].dropna().tolist()
            else:
                raise ValueError(f"DataFrame must have '{url_column}' column")
        
        # Filter valid URLs
        urls = [url for url in urls if url and str(url).startswith('http')]
        
        if not urls:
            logger.warning("No valid URLs to scrape")
            return pd.DataFrame()
        
        logger.info(f"Starting to scrape {len(urls)} URLs")
        
        # Initialize crawler if not already done
        if not self._is_initialized:
            await self.initialize_crawler()
        
        try:
            # Run the crawler
            await self._crawler.run(urls)
        finally:
            # Always close resources
            await self.close()
        
        # Log statistics
        logger.info(f"Scraped {len(self.members_data)} board member records")
        logger.info(f"Succeeded: {self._stats['urls_succeeded']}, Failed: {self._stats['urls_failed']}")
        
        if self.errors:
            logger.warning(f"Encountered {len(self.errors)} errors")
        
        return pd.DataFrame(self.members_data)
    
    async def scrape_from_csv(
        self,
        csv_path: str,
        url_column: str = 'url'
    ) -> pd.DataFrame:
        """
        Scrape board member information from URLs in a CSV file.
        
        Args:
            csv_path: Path to CSV file containing URLs
            url_column: Name of column containing URLs
            
        Returns:
            DataFrame with scraped board member information
        """
        try:
            df = pd.read_csv(csv_path)
            return await self.scrape_urls(df, url_column)
        except FileNotFoundError:
            logger.error(f"CSV file not found: {csv_path}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error reading CSV file: {e}")
            return pd.DataFrame()
    
    # ============== Export Methods ==============
    
    def export_to_excel(
        self,
        df: Optional[pd.DataFrame] = None,
        file_path: str = 'board_members.xlsx',
        include_summary: bool = True,
        include_errors: bool = True
    ) -> None:
        """
        Export board member data to Excel.
        
        Args:
            df: DataFrame to export (uses internal data if None)
            file_path: Output Excel file path
            include_summary: Whether to include summary statistics
            include_errors: Whether to include error log
        """
        try:
            if df is None:
                df = pd.DataFrame(self.members_data)
            
            if df.empty:
                logger.warning("No data to export")
                return
            
            with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
                # Main data
                df.to_excel(writer, sheet_name='Board Members', index=False)
                
                # Summary
                if include_summary:
                    summary = self._create_summary(df)
                    summary.to_excel(writer, sheet_name='Summary', index=False)
                
                # Network stats
                if self.network and self.network.number_of_nodes() > 0:
                    network_stats = self._create_network_stats()
                    network_stats.to_excel(writer, sheet_name='Network Stats', index=False)
                
                # Errors
                if include_errors and self.errors:
                    errors_df = pd.DataFrame([e.to_dict() for e in self.errors])
                    errors_df.to_excel(writer, sheet_name='Errors', index=False)
            
            logger.info(f"Data exported to {file_path}")
            
        except Exception as e:
            logger.error(f"Error exporting to Excel: {e}")
    
    def export_to_csv(
        self,
        df: Optional[pd.DataFrame] = None,
        file_path: str = 'board_members.csv'
    ) -> None:
        """
        Export board member data to CSV.
        
        Args:
            df: DataFrame to export (uses internal data if None)
            file_path: Output CSV file path
        """
        try:
            if df is None:
                df = pd.DataFrame(self.members_data)
            
            if df.empty:
                logger.warning("No data to export")
                return
            
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            logger.info(f"Data exported to {file_path}")
            
        except Exception as e:
            logger.error(f"Error exporting to CSV: {e}")
    
    def _create_summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """Create summary statistics"""
        summary = {
            'Metric': [
                'Total Records',
                'Unique Companies',
                'Unique Board Members',
                'Date Range',
                'Independent Members',
                'Members with Multiple Executive Roles',
                'URLs Processed',
                'URLs Succeeded',
                'URLs Failed',
                'Error Count'
            ],
            'Value': [
                len(df),
                df['company'].nunique() if 'company' in df.columns else 0,
                df['new_member'].nunique() if 'new_member' in df.columns else 0,
                f"{df['date'].min()} - {df['date'].max()}" if 'date' in df.columns and len(df) > 0 else "",
                df['is_independent'].sum() if 'is_independent' in df.columns else 0,
                df['has_multiple_executive'].sum() if 'has_multiple_executive' in df.columns else 0,
                self._stats['urls_processed'],
                self._stats['urls_succeeded'],
                self._stats['urls_failed'],
                len(self.errors)
            ]
        }
        return pd.DataFrame(summary)
    
    def _create_network_stats(self) -> pd.DataFrame:
        """Create network statistics"""
        companies = [n for n, d in self.network.nodes(data=True) if d.get('node_type') == 'company']
        people = [n for n, d in self.network.nodes(data=True) if d.get('node_type') == 'person']
        
        stats = {
            'Metric': [
                'Total Nodes',
                'Total Edges',
                'Companies',
                'Board Members',
                'Average Connections per Person',
                'Network Density'
            ],
            'Value': [
                self.network.number_of_nodes(),
                self.network.number_of_edges(),
                len(companies),
                len(people),
                round(sum(dict(self.network.degree()).values()) / max(len(people), 1), 2),
                round(nx.density(self.network), 4) if self.network.number_of_nodes() > 0 else 0
            ]
        }
        return pd.DataFrame(stats)
    
    def get_error_summary(self) -> Dict:
        """Get summary of all errors"""
        from collections import Counter
        
        error_counts = Counter(e.error_type.value for e in self.errors)
        
        return {
            'total_errors': len(self.errors),
            'by_type': dict(error_counts),
            'recoverable': sum(1 for e in self.errors if e.recoverable),
            'failed_urls': [e.url for e in self.errors]
        }
    
    def get_stats(self) -> Dict:
        """Get scraping statistics"""
        return {
            **self._stats,
            'error_summary': self.get_error_summary()
        }
    
    def export_errors(self, filepath: str = 'scraping_errors.json') -> None:
        """Export errors to JSON for analysis"""
        import json
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(
                [e.to_dict() for e in self.errors],
                f,
                ensure_ascii=False,
                indent=2
            )
        logger.info(f"Errors exported to {filepath}")
    
    def visualize_network(
        self,
        output_file: str = 'board_network.html',
        height: str = '750px',
        width: str = '100%'
    ) -> None:
        """
        Create an interactive network visualization.
        
        Args:
            output_file: Output HTML file path
            height: Visualization height
            width: Visualization width
        """
        try:
            if not self.network or self.network.number_of_nodes() == 0:
                logger.warning("No network data to visualize")
                return
            
            from pyvis.network import Network
            
            net = Network(height=height, width=width, notebook=False)
            
            # Add nodes
            for node, attrs in self.network.nodes(data=True):
                if attrs.get('node_type') == 'company':
                    net.add_node(
                        node,
                        label=node,
                        color='#ff7f0e',
                        size=30,
                        title=f"Company: {node}"
                    )
                else:
                    title = f"Person: {node}\nDegree: {attrs.get('degree', 'N/A')}\nMajor: {attrs.get('major', 'N/A')}"
                    net.add_node(
                        node,
                        label=node,
                        color='#1f77b4',
                        size=20,
                        title=title
                    )
            
            # Add edges
            for u, v, attrs in self.network.edges(data=True):
                title = f"Position: {attrs.get('position', 'N/A')}\nYear: {attrs.get('year', 'N/A')}"
                net.add_edge(u, v, title=title)
            
            net.barnes_hut()
            net.save_graph(output_file)
            logger.info(f"Network visualization saved to {output_file}")
            
        except ImportError:
            logger.error(
                "pyvis is required for network visualization. "
                "Install with: pip install pyvis"
            )
        except Exception as e:
            logger.error(f"Error creating visualization: {e}")
    
    def reset(self) -> None:
        """Reset scraper state for new scraping session"""
        self.members_data = []
        self.network = nx.Graph()
        self.errors = []
        self._stats = {
            'urls_processed': 0,
            'urls_succeeded': 0,
            'urls_failed': 0,
            'members_extracted': 0,
            'start_time': None,
            'end_time': None
        }
        logger.info("Scraper state reset")